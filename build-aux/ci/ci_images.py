#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Resolve, pull, build, and publish @sha256-pinned prebuilt per-distro CI images (GNOME-CI-CACHING.md #2).

Security and freshness invariants:
- Only scheduled nightly and main-branch jobs may pass --push (never pull_request).
- CI consumers pull prebuilt GHCR images strictly by @sha256 digest from build-aux/ci/image-pins.json.
- Unpinned rolling image references (nightly schedule legs and rpm-canary) never resolve to prebuilt images.
- When dependency declaration files in a PR differ from the pinned input_hash (or before a digest is
  published in GHCR), consumers build locally from the @sha256-pinned raw base image so missing or
  removed build dependencies fail in that PR.
- Registry logins run inside a disposable DOCKER_CONFIG/REGISTRY_AUTH_FILE directory per invocation,
  never writing ephemeral GITHUB_TOKENs into ~/.docker/config.json on self-hosted runners.
- Clean-install smoke containers (container_offline_smoke) never call this helper and stay on bare bases.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Mapping
from pathlib import Path

_CI_DIR = str(Path(__file__).resolve().parent)
if _CI_DIR not in sys.path:
    sys.path.insert(0, _CI_DIR)
import ci_image_core  # noqa: E402

ROOT = ci_image_core.ROOT
PINS_FILE = ci_image_core.PINS_FILE
GHCR_PREFIX = ci_image_core.GHCR_PREFIX
SOURCE_REPO_URL = ci_image_core.SOURCE_REPO_URL
DIGEST_RE = ci_image_core.DIGEST_RE
INPUT_HASH_RE = ci_image_core.INPUT_HASH_RE
ALLOWED_PUSH_EVENTS = ci_image_core.ALLOWED_PUSH_EVENTS
PrebuiltSpec = ci_image_core.PrebuiltSpec
UBUNTU_BUILD_SPEC = ci_image_core.UBUNTU_BUILD_SPEC
detect_engine = ci_image_core.detect_engine


def _source_inputs(base: str) -> tuple[str, ...]:
    extra: tuple[str, ...] = ()
    if base == "registry.opensuse.org/opensuse/tumbleweed:latest":
        extra = ("build-aux/ci/prepare_tumbleweed.sh",)
    elif base == "alpine:edge":
        extra = ("build-aux/ci/alpine_edge_install.sh",)
    return (
        "build-aux/ci/source.Dockerfile",
        "build-aux/install-deps.sh",
        "build-aux/check-distros.sh",
        "build-aux/ci/retry.sh",
        *extra,
    )


def _rpm_inputs(base: str) -> tuple[str, ...]:
    extra = ("build-aux/ci/prepare_tumbleweed.sh",) if base.endswith("tumbleweed:latest") else ()
    return (
        "build-aux/rpm/cassini-talk.spec",
        "build-aux/rpm/Dockerfile",
        "build-aux/ci/retry.sh",
        *extra,
    )


PREBUILT_SPECS: tuple[PrebuiltSpec, ...] = (
    UBUNTU_BUILD_SPEC,
    PrebuiltSpec(
        key="source-ubuntu-26.04",
        role="source",
        base="ubuntu:26.04",
        dockerfile="build-aux/ci/source.Dockerfile",
        context=".",
        inputs=_source_inputs("ubuntu:26.04"),
        marker="/etc/cassini-deps-installed",
    ),
    PrebuiltSpec(
        key="source-debian-testing",
        role="source",
        base="debian:testing",
        dockerfile="build-aux/ci/source.Dockerfile",
        context=".",
        inputs=_source_inputs("debian:testing"),
        marker="/etc/cassini-deps-installed",
    ),
    PrebuiltSpec(
        key="source-fedora-44",
        role="source",
        base="fedora:44",
        dockerfile="build-aux/ci/source.Dockerfile",
        context=".",
        inputs=_source_inputs("fedora:44"),
        marker="/etc/cassini-deps-installed",
    ),
    PrebuiltSpec(
        key="source-fedora-latest",
        role="source",
        base="fedora:latest",
        dockerfile="build-aux/ci/source.Dockerfile",
        context=".",
        inputs=_source_inputs("fedora:latest"),
        marker="/etc/cassini-deps-installed",
    ),
    PrebuiltSpec(
        key="source-opensuse-tumbleweed",
        role="source",
        base="registry.opensuse.org/opensuse/tumbleweed:latest",
        dockerfile="build-aux/ci/source.Dockerfile",
        context=".",
        inputs=_source_inputs("registry.opensuse.org/opensuse/tumbleweed:latest"),
        marker="/etc/cassini-deps-installed",
    ),
    PrebuiltSpec(
        key="source-archlinux",
        role="source",
        base="archlinux:latest",
        dockerfile="build-aux/ci/source.Dockerfile",
        context=".",
        inputs=_source_inputs("archlinux:latest"),
        marker="/etc/cassini-deps-installed",
    ),
    PrebuiltSpec(
        key="source-alpine-edge",
        role="source",
        base="alpine:edge",
        dockerfile="build-aux/ci/source.Dockerfile",
        context=".",
        inputs=_source_inputs("alpine:edge"),
        marker="/etc/cassini-deps-installed",
    ),
    PrebuiltSpec(
        key="deb-ubuntu-26.04",
        role="deb",
        base="ubuntu:26.04",
        dockerfile="build-aux/deb/Dockerfile",
        context="debian",
        inputs=("debian/control", "build-aux/deb/Dockerfile"),
        marker="/etc/dpkg/dpkg.cfg.d/zz-cassini-locales",
    ),
    PrebuiltSpec(
        key="deb-debian-testing",
        role="deb",
        base="debian:testing",
        dockerfile="build-aux/deb/Dockerfile",
        context="debian",
        inputs=("debian/control", "build-aux/deb/Dockerfile"),
        marker="/etc/dpkg/dpkg.cfg.d/zz-cassini-locales",
    ),
    PrebuiltSpec(
        key="rpm-fedora-44",
        role="rpm",
        base="fedora:44",
        dockerfile="build-aux/rpm/Dockerfile",
        context=".",
        inputs=_rpm_inputs("fedora:44"),
        marker="/etc/cassini-rpm-deps-installed",
    ),
    PrebuiltSpec(
        key="rpm-opensuse-tumbleweed",
        role="rpm",
        base="registry.opensuse.org/opensuse/tumbleweed:latest",
        dockerfile="build-aux/rpm/Dockerfile",
        context=".",
        inputs=_rpm_inputs("registry.opensuse.org/opensuse/tumbleweed:latest"),
        marker="/etc/cassini-rpm-deps-installed",
    ),
    PrebuiltSpec(
        key="arch-archlinux",
        role="arch",
        base="archlinux:latest",
        dockerfile="build-aux/arch/Dockerfile",
        context=".",
        inputs=("build-aux/arch/PKGBUILD", "build-aux/arch/Dockerfile", "build-aux/ci/retry.sh"),
        marker="/etc/cassini-arch-deps-installed",
    ),
)

SPECS_BY_KEY: dict[str, PrebuiltSpec] = {spec.key: spec for spec in PREBUILT_SPECS}
SPECS_BY_ROLE_AND_BASE: dict[tuple[str, str], PrebuiltSpec] = {(spec.role, spec.base): spec for spec in PREBUILT_SPECS}
PREBUILT_INPUT_FILES: tuple[str, ...] = tuple(sorted({path for spec in PREBUILT_SPECS for path in spec.inputs}))


def normalize_input_bytes(rel_path: str, raw: bytes) -> bytes:
    """Ignore package version stamps that daily_test_release.py / release bumps rewrite."""
    if rel_path == "build-aux/rpm/cassini-talk.spec":
        return re.sub(rb"^Version:\s*\S+", b"Version: 0", raw, count=1, flags=re.M)
    if rel_path == "build-aux/arch/PKGBUILD":
        return re.sub(rb"^pkgver=\S+", b"pkgver=0", raw, count=1, flags=re.M)
    return raw


def compute_input_hash(spec: PrebuiltSpec, base_digest: str, root: Path = ROOT) -> str:
    return ci_image_core.compute_input_hash(spec, base_digest, root=root, normalizer=normalize_input_bytes)


def _lookup_spec(role: str, base_ref: str) -> PrebuiltSpec | None:
    if "@" not in base_ref:
        return None
    base_tag, _digest = base_ref.split("@", 1)
    return SPECS_BY_ROLE_AND_BASE.get((role, base_tag))


def resolve_prebuilt(
    role: str,
    base_ref: str,
    *,
    root: Path = ROOT,
    env: Mapping[str, str] | None = None,
) -> str | None:
    return ci_image_core.resolve_spec_prebuilt(
        _lookup_spec(role, base_ref),
        base_ref,
        root=root,
        env=env,
        normalizer=normalize_input_bytes,
    )


def pull_prebuilt(
    role: str,
    base_ref: str,
    *,
    root: Path = ROOT,
    env: Mapping[str, str] | None = None,
    runner=subprocess.run,
) -> str | None:
    return ci_image_core.pull_spec_prebuilt(
        _lookup_spec(role, base_ref),
        base_ref,
        root=root,
        env=env,
        normalizer=normalize_input_bytes,
        runner=runner,
    )


def prepare_image(
    role: str,
    base_ref: str,
    *,
    root: Path = ROOT,
    env: Mapping[str, str] | None = None,
    runner=subprocess.run,
) -> str:
    if "@" not in base_ref:
        raise ValueError(f"prepare requires a @sha256-pinned base reference, got {base_ref!r}")
    base_tag, _digest = base_ref.split("@", 1)
    spec = SPECS_BY_ROLE_AND_BASE.get((role, base_tag))
    if spec is None:
        raise ValueError(f"unknown prebuilt image role/base: ({role!r}, {base_tag!r})")
    return ci_image_core.prepare_spec_image(
        spec,
        base_ref,
        root=root,
        env=env,
        normalizer=normalize_input_bytes,
        runner=runner,
    )


def _verify_built_image(engine: str, spec: PrebuiltSpec, image_ref: str, *, runner=subprocess.run) -> None:
    ep = subprocess.check_output(
        [engine, "image", "inspect", "-f", "{{json .Config.Entrypoint}}", image_ref],
        text=True,
    )
    parsed_ep = json.loads(ep.strip() or "null")
    if parsed_ep not in (None, []):
        raise RuntimeError(f"{image_ref}: unexpected image ENTRYPOINT {parsed_ep!r}")
    check = runner(
        [
            engine,
            "run",
            "--rm",
            "--network",
            "none",
            image_ref,
            "sh",
            "-ec",
            f"test -r /etc/os-release && test -e {spec.marker}",
        ],
        stdout=sys.stderr,
    )
    if check.returncode != 0:
        raise RuntimeError(f"{image_ref}: offline verification failed for marker {spec.marker}")


def inspect_remote_digest(image_ref: str, *, run_env: Mapping[str, str] | None = None) -> str | None:
    kwargs = {"env": dict(run_env)} if run_env is not None else {}
    try:
        raw = subprocess.check_output(
            ["docker", "buildx", "imagetools", "inspect", "--raw", image_ref],
            stderr=subprocess.DEVNULL,
            **kwargs,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    return digest if DIGEST_RE.fullmatch(digest) else None


def enforce_push_policy(env: Mapping[str, str]) -> None:
    event = env.get("GITHUB_EVENT_NAME", "")
    ref = env.get("GITHUB_REF", "")
    if event not in ALLOWED_PUSH_EVENTS or ref != "refs/heads/main":
        raise RuntimeError(
            f"GHCR image pushes are restricted to main schedule/push/workflow_dispatch jobs "
            f"(got GITHUB_EVENT_NAME={event!r}, GITHUB_REF={ref!r})"
        )


def _append_summary(env: Mapping[str, str], line: str) -> None:
    summary = env.get("GITHUB_STEP_SUMMARY")
    if summary:
        with Path(summary).open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")


def build_and_push_image(
    key: str,
    *,
    push: bool = False,
    rebuild: bool = False,
    root: Path = ROOT,
    env: Mapping[str, str] | None = None,
) -> int:
    environ = os.environ if env is None else env
    if key not in SPECS_BY_KEY:
        raise KeyError(f"unknown prebuilt image key: {key!r}")
    if push:
        enforce_push_policy(environ)
    spec = SPECS_BY_KEY[key]
    pins = json.loads((root / PINS_FILE).read_text())
    base_digest = pins["images"][spec.base]["digest"]
    base_ref = f"{spec.base}@{base_digest}"
    input_hash = compute_input_hash(spec, base_digest, root=root)
    tag_ref = f"{spec.repository}:{input_hash}"

    engine = detect_engine(environ)
    with ci_image_core.isolated_ghcr_env(engine, environ, require_login=push) as auth_env:
        if push and not rebuild:
            existing = inspect_remote_digest(tag_ref, run_env=auth_env)
            if existing is not None:
                msg = f"`{spec.key}`: already published `{spec.repository}@{existing}` (tag `{input_hash}`)"
                print(msg)
                _append_summary(environ, f"- {msg}")
                return 0

        if not ci_image_core.pull_image(engine, base_ref):
            raise RuntimeError(f"failed to pull pinned base {base_ref}")
        build_cmd = [
            engine,
            "build",
            "--no-cache",
            "--network",
            "host",
            "-f",
            str(root / spec.dockerfile),
            "--build-arg",
            f"BASE={base_ref}",
            "--label",
            f"org.opencontainers.image.source={SOURCE_REPO_URL}",
            "-t",
            tag_ref,
            str(root / spec.context),
        ]
        if not ci_image_core.run_with_retry(build_cmd):
            raise RuntimeError(f"failed to build {tag_ref} from {base_ref}")
        _verify_built_image(engine, spec, tag_ref)

        if not push:
            print(f"Built and verified {tag_ref} locally (push disabled).")
            return 0

        if not ci_image_core.run_with_retry([engine, "push", tag_ref], run_env=auth_env):
            raise RuntimeError(f"failed to push {tag_ref}")
        pushed_digest = inspect_remote_digest(tag_ref, run_env=auth_env)
        if pushed_digest is None:
            raise RuntimeError(f"pushed {tag_ref} but could not resolve remote manifest digest")
        msg = f"`{spec.key}`: published `{spec.repository}@{pushed_digest}` (tag `{input_hash}`)"
        print(msg)
        _append_summary(environ, f"- {msg}")
        return 0


def main(argv: list[str] | None = None, *, root: Path = ROOT) -> int:
    # Re-export time so tests monkeypatching ci_images.time or ci_image_core.time stay simple.
    _ = time
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_resolve = sub.add_parser("resolve", help="Print @sha256-pinned prebuilt GHCR image if valid and current")
    p_resolve.add_argument("role", choices=("ubuntu-build", "source", "deb", "rpm", "arch"))
    p_resolve.add_argument("base")

    p_pull = sub.add_parser("pull", help="Resolve and pull @sha256-pinned prebuilt GHCR image, exiting 1 on miss")
    p_pull.add_argument("role", choices=("ubuntu-build", "source", "deb", "rpm", "arch"))
    p_pull.add_argument("base")

    p_prepare = sub.add_parser("prepare", help="Pull @sha256-pinned GHCR image or build locally from pinned base")
    p_prepare.add_argument("role", choices=("ubuntu-build", "source", "deb", "rpm", "arch"))
    p_prepare.add_argument("base")

    sub.add_parser("matrix", help="Print GitHub Actions JSON matrix for all prebuilt CI images")

    p_build = sub.add_parser("build", help="Build (and optionally push on main/schedule) one prebuilt CI image")
    p_build.add_argument("key", choices=tuple(SPECS_BY_KEY))
    p_build.add_argument("--push", action="store_true", help="Push to GHCR (restricted to main schedule/push/dispatch)")
    p_build.add_argument("--rebuild", action="store_true", help="Rebuild --no-cache even if tag exists in GHCR")

    args = parser.parse_args(argv)
    if args.cmd == "resolve":
        resolved = resolve_prebuilt(args.role, args.base, root=root)
        if resolved is not None:
            print(resolved)
        return 0
    if args.cmd == "pull":
        pulled = pull_prebuilt(args.role, args.base, root=root)
        if pulled is None:
            return 1
        print(pulled)
        return 0
    if args.cmd == "prepare":
        print(prepare_image(args.role, args.base, root=root))
        return 0
    if args.cmd == "matrix":
        matrix = {"include": [{"key": spec.key, "base": spec.base} for spec in PREBUILT_SPECS]}
        print(json.dumps(matrix, separators=(",", ":")))
        return 0
    return build_and_push_image(args.key, push=args.push, rebuild=args.rebuild, root=root)


if __name__ == "__main__":
    raise SystemExit(main())
