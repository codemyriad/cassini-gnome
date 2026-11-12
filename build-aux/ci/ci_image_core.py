#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared engine for @sha256-pinned GHCR prebuilt CI images (GNOME-CI-CACHING.md #2).

Kept free of distro-specific packaging paths so build.yml's minimal Ubuntu build
helper only pulls minimal-build inputs into static dependency scans (boards_inputs.py).
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PINS_FILE = Path("build-aux/ci/image-pins.json")
GHCR_PREFIX = "ghcr.io/codemyriad/cassini-gnome/ci-"
SOURCE_REPO_URL = "https://github.com/codemyriad/cassini-gnome"

DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
INPUT_HASH_RE = re.compile(r"^[0-9a-f]{16}$")
ALLOWED_PUSH_EVENTS = frozenset({"push", "schedule", "workflow_dispatch"})


@dataclasses.dataclass(frozen=True)
class PrebuiltSpec:
    key: str
    role: str
    base: str
    dockerfile: str
    context: str
    inputs: tuple[str, ...]
    marker: str

    @property
    def repository(self) -> str:
        return f"{GHCR_PREFIX}{self.key}"


UBUNTU_BUILD_SPEC = PrebuiltSpec(
    key="ubuntu-build",
    role="ubuntu-build",
    base="ubuntu:26.04",
    dockerfile="build-aux/ci/ubuntu-build.Dockerfile",
    context=".",
    inputs=(
        "build-aux/ci/ubuntu-build.Dockerfile",
        "build-aux/ci/install-minimal-build-deps.sh",
        "build-aux/ci/apt-install-bounded.sh",
    ),
    marker="/etc/cassini-minimal-build-deps",
)


def compute_input_hash(
    spec: PrebuiltSpec,
    base_digest: str,
    *,
    root: Path = ROOT,
    normalizer: Callable[[str, bytes], bytes] | None = None,
) -> str:
    if not DIGEST_RE.fullmatch(base_digest):
        raise ValueError(f"invalid base digest for {spec.key}: {base_digest!r}")
    hasher = hashlib.sha256()
    for rel_path in spec.inputs:
        raw = (root / rel_path).read_bytes()
        normalized = normalizer(rel_path, raw) if normalizer is not None else raw
        file_digest = hashlib.sha256(normalized).hexdigest()
        hasher.update(f"{rel_path}\0{file_digest}\n".encode())
    hasher.update(f"base={spec.base}@{base_digest}\n".encode())
    return hasher.hexdigest()[:16]


def detect_engine(env: Mapping[str, str] | None = None) -> str:
    environ = os.environ if env is None else env
    configured = environ.get("CONTAINER_ENGINE", "").strip()
    if configured:
        return configured
    for candidate in ("podman", "docker"):
        if shutil.which(candidate):
            return candidate
    raise RuntimeError("neither podman nor docker is installed")


@contextlib.contextmanager
def isolated_ghcr_env(
    engine: str,
    env: Mapping[str, str] | None = None,
    *,
    require_login: bool = False,
    runner=subprocess.run,
) -> Iterator[dict[str, str] | None]:
    """Yield an isolated DOCKER_CONFIG/REGISTRY_AUTH_FILE env when GHCR token auth is configured."""
    environ = os.environ if env is None else env
    token = (environ.get("GHCR_READ_TOKEN") or environ.get("GITHUB_TOKEN") or "").strip()
    actor = (environ.get("GITHUB_ACTOR") or "").strip()
    if not token or not actor:
        if require_login:
            raise RuntimeError("GITHUB_TOKEN and GITHUB_ACTOR are required for GHCR push authentication")
        yield None
        return

    with tempfile.TemporaryDirectory(prefix="cassini-ghcr-auth-") as tmpdir:
        auth_env = dict(environ)
        auth_env["DOCKER_CONFIG"] = tmpdir
        auth_env["REGISTRY_AUTH_FILE"] = str(Path(tmpdir) / "config.json")
        res = runner(
            [engine, "login", "ghcr.io", "-u", actor, "--password-stdin"],
            input=token,
            text=True,
            capture_output=True,
            env=auth_env,
        )
        if res.returncode != 0:
            if require_login:
                raise RuntimeError(f"ghcr.io login failed with exit code {res.returncode}")
            print(
                f"ci-images: optional ghcr.io login returned {res.returncode}; trying anonymous pull",
                file=sys.stderr,
            )
            yield None
            return
        yield auth_env


def resolve_spec_prebuilt(
    spec: PrebuiltSpec | None,
    base_ref: str,
    *,
    root: Path = ROOT,
    env: Mapping[str, str] | None = None,
    normalizer: Callable[[str, bytes], bytes] | None = None,
) -> str | None:
    environ = os.environ if env is None else env
    if environ.get("CASSINI_NO_PREBUILT_CI_IMAGES") == "1":
        return None
    if "@" not in base_ref or spec is None:
        return None
    _base_tag, base_digest = base_ref.split("@", 1)
    if not DIGEST_RE.fullmatch(base_digest):
        return None
    pins = json.loads((root / PINS_FILE).read_text())
    expected_base = pins.get("images", {}).get(spec.base, {}).get("digest")
    if expected_base != base_digest:
        print(
            f"ci-images: {spec.key} requested base {base_digest[:19]} differs from pinned {str(expected_base)[:19]}; "
            "using raw base",
            file=sys.stderr,
        )
        return None
    entry = (pins.get("prebuilt_images") or {}).get(spec.key)
    if not isinstance(entry, dict):
        return None
    pinned_digest = entry.get("digest")
    if not (isinstance(pinned_digest, str) and DIGEST_RE.fullmatch(pinned_digest)):
        print(
            f"ci-images: {spec.key} has no published GHCR digest in {PINS_FILE}; building from raw base",
            file=sys.stderr,
        )
        return None
    if (
        entry.get("repository") != spec.repository
        or entry.get("base") != spec.base
        or entry.get("base_digest") != base_digest
    ):
        print(f"ci-images: {spec.key} metadata mismatch in {PINS_FILE}; building from raw base", file=sys.stderr)
        return None
    current_hash = compute_input_hash(spec, base_digest, root=root, normalizer=normalizer)
    if entry.get("input_hash") != current_hash:
        print(
            f"ci-images: {spec.key} dependency inputs changed ({current_hash} != {entry.get('input_hash')}); "
            "building from raw base",
            file=sys.stderr,
        )
        return None
    return f"{spec.repository}@{pinned_digest}"


def run_with_retry(
    cmd: list[str],
    *,
    attempts: int = 3,
    delay_seconds: float = 2.0,
    run_env: Mapping[str, str] | None = None,
    runner=subprocess.run,
) -> bool:
    kwargs = {"env": dict(run_env)} if run_env is not None else {}
    for attempt in range(1, attempts + 1):
        res = runner(cmd, **kwargs)
        if res.returncode == 0:
            return True
        if attempt < attempts:
            print(
                f"ci-images: command failed (attempt {attempt}/{attempts}, rc={res.returncode}): {' '.join(cmd)}",
                file=sys.stderr,
            )
            time.sleep(delay_seconds * attempt)
    return False


def pull_image(
    engine: str,
    image_ref: str,
    *,
    run_env: Mapping[str, str] | None = None,
    runner=subprocess.run,
) -> bool:
    inspect = runner([engine, "image", "inspect", image_ref], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if inspect.returncode == 0:
        return True
    return run_with_retry([engine, "pull", "-q", image_ref], run_env=run_env, runner=runner)


def pull_spec_prebuilt(
    spec: PrebuiltSpec | None,
    base_ref: str,
    *,
    root: Path = ROOT,
    env: Mapping[str, str] | None = None,
    normalizer: Callable[[str, bytes], bytes] | None = None,
    runner=subprocess.run,
) -> str | None:
    environ = os.environ if env is None else env
    resolved = resolve_spec_prebuilt(spec, base_ref, root=root, env=environ, normalizer=normalizer)
    if resolved is None:
        return None
    engine = detect_engine(environ)
    inspect = runner([engine, "image", "inspect", resolved], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if inspect.returncode == 0:
        return resolved
    with isolated_ghcr_env(engine, environ, runner=runner) as auth_env:
        if run_with_retry([engine, "pull", "-q", resolved], run_env=auth_env, runner=runner):
            return resolved
    print(f"::warning::ci-images: failed to pull {resolved}; falling back to raw base {base_ref}", file=sys.stderr)
    return None


def prepare_spec_image(
    spec: PrebuiltSpec,
    base_ref: str,
    *,
    root: Path = ROOT,
    env: Mapping[str, str] | None = None,
    normalizer: Callable[[str, bytes], bytes] | None = None,
    runner=subprocess.run,
) -> str:
    environ = os.environ if env is None else env
    if "@" not in base_ref:
        raise ValueError(f"prepare requires a @sha256-pinned base reference, got {base_ref!r}")
    base_tag, base_digest = base_ref.split("@", 1)
    if not DIGEST_RE.fullmatch(base_digest):
        raise ValueError(f"prepare requires a valid @sha256 digest, got {base_ref!r}")
    if base_tag != spec.base:
        raise ValueError(f"expected base {spec.base!r} for {spec.key}, got {base_tag!r}")

    pulled = pull_spec_prebuilt(spec, base_ref, root=root, env=environ, normalizer=normalizer, runner=runner)
    if pulled is not None:
        return pulled

    engine = detect_engine(environ)
    if not pull_image(engine, base_ref, runner=runner):
        raise RuntimeError(f"failed to pull pinned base image {base_ref}")
    local_tag = f"cassini-ci-local:{spec.key}"
    build_cmd = [
        engine,
        "build",
        "--network",
        "host",
        "-f",
        str(root / spec.dockerfile),
        "--build-arg",
        f"BASE={base_ref}",
        "-t",
        local_tag,
        str(root / spec.context),
    ]
    # stdout is captured for $GITHUB_ENV; send docker build output to stderr.
    for attempt in range(1, 4):
        res = runner(build_cmd, stdout=sys.stderr)
        if res.returncode == 0:
            break
        if attempt == 3:
            raise RuntimeError(f"failed to build local CI image {local_tag} from {base_ref}")
        time.sleep(2.0 * attempt)
    return local_tag
