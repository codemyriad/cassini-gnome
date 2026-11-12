#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Pin CI container images by @sha256 digest and verify zero floating references.

    python3 build-aux/ci/pin_images.py --check   # verify repo files match build-aux/ci/image-pins.json
    python3 build-aux/ci/pin_images.py --bump    # query OCI registries and rewrite pins in one step

Intentional exception: .github/workflows/rpm-canary.yml probes unpinned
fedora:latest nightly outside the merge and release gates (D66 rule 3).
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PINS_FILE = Path("build-aux/ci/image-pins.json")

IMAGES = (
    "ubuntu:26.04",
    "debian:testing",
    "fedora:44",
    "fedora:latest",
    "registry.opensuse.org/opensuse/tumbleweed:latest",
    "archlinux:latest",
    "alpine:edge",
)

HARNESS_BASE_IMAGES = ("ubuntu:26.04", "debian:testing")

# Files whose container image references are managed by --bump and verified by --check.
IMAGE_TARGETS: dict[str, tuple[str, ...]] = {
    ".github/workflows/build.yml": ("ubuntu:26.04",),
    ".github/workflows/meson-tests.yml": ("ubuntu:26.04",),
    ".github/workflows/source.yml": IMAGES,
    ".github/workflows/deb.yml": ("ubuntu:26.04", "debian:testing"),
    ".github/workflows/packages.yml": ("fedora:44", "registry.opensuse.org/opensuse/tumbleweed:latest"),
    "build-aux/check-distros.sh": (
        "debian:testing",
        "ubuntu:26.04",
        "fedora:latest",
        "registry.opensuse.org/opensuse/tumbleweed:latest",
        "archlinux:latest",
        "alpine:edge",
    ),
    "build-aux/deb/build.sh": ("ubuntu:26.04",),
    "build-aux/deb/Dockerfile": ("ubuntu:26.04",),
    "build-aux/rpm/build.sh": ("fedora:44",),
    "build-aux/arch/build.sh": ("archlinux:latest",),
    "Makefile": ("registry.opensuse.org/opensuse/tumbleweed:latest",),
    "tests/recording/Audio.Dockerfile": ("ubuntu:26.04",),
}

HARNESS_WORKFLOWS = (
    ".github/workflows/background-apps.yml",
    ".github/workflows/deb.yml",
    ".github/workflows/whistle.yml",
)

CANARY_EXEMPT_WORKFLOWS = {".github/workflows/rpm-canary.yml"}
HARNESS_STAGE_DOCKERFILES = {
    "tests/desktop/Dockerfile",
    "tests/build-refresh/Dockerfile",
    "tests/browser-setup/Dockerfile",
    "tests/whistle/Containerfile",
}

DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def resolve_image(tag: str) -> dict[str, str]:
    raw = subprocess.check_output(["docker", "buildx", "imagetools", "inspect", "--raw", tag])
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    doc = json.loads(raw)
    amd64_digest = digest
    if isinstance(doc, dict) and "manifests" in doc:
        matches = [
            m["digest"]
            for m in doc["manifests"]
            if m.get("platform", {}).get("os") == "linux" and m.get("platform", {}).get("architecture") == "amd64"
        ]
        if len(matches) != 1:
            raise RuntimeError(f"{tag}: expected 1 linux/amd64 manifest, got {len(matches)}")
        amd64_digest = matches[0]
    if not DIGEST_RE.fullmatch(digest) or not DIGEST_RE.fullmatch(amd64_digest):
        raise RuntimeError(f"{tag}: invalid resolved digest ({digest}, {amd64_digest})")
    return {"digest": digest, "amd64_digest": amd64_digest}


def load_pins(root: Path = ROOT) -> dict:
    data = json.loads((root / PINS_FILE).read_text())
    images = data.get("images")
    if not isinstance(images, dict) or set(images) != set(IMAGES):
        raise ValueError(f"{PINS_FILE}: expected images keys {sorted(IMAGES)}")
    for tag, entry in images.items():
        if not isinstance(entry, dict):
            raise ValueError(f"{PINS_FILE}: {tag} entry must be an object")
        for key in ("digest", "amd64_digest"):
            if not isinstance(entry.get(key), str) or not DIGEST_RE.fullmatch(entry[key]):
                raise ValueError(f"{PINS_FILE}: {tag}.{key} is not a valid sha256 digest")
    return data


def harness_bases_value(pins: dict) -> str:
    return " ".join(f"{tag}@{pins['images'][tag]['amd64_digest']}" for tag in HARNESS_BASE_IMAGES)


def rewrite_content(rel_path: str, text: str, pins: dict) -> str:
    if rel_path in HARNESS_WORKFLOWS:
        hb = harness_bases_value(pins)
        text = re.sub(
            r"^(\s*CASSINI_HARNESS_BASES:\s*).*$",
            rf"\g<1>{hb}",
            text,
            flags=re.M,
        )
    tags = IMAGE_TARGETS.get(rel_path, ())
    lines = text.splitlines(keepends=True)
    out_lines = []
    for line in lines:
        stripped = line.lstrip()
        # Leave comments, human descriptions, deb.yml startsWith() guards, and stripped tag comparisons untouched.
        if (
            stripped.startswith(("#", "description:"))
            or "startsWith(matrix.base," in line
            or "${image%@sha256:*}" in line
            or "runs-on:" in line
        ):
            out_lines.append(line)
            continue
        # Only rewrite CASSINI_HARNESS_BASES via the dedicated block above.
        if stripped.startswith("CASSINI_HARNESS_BASES:"):
            out_lines.append(line)
            continue
        updated = line
        for tag in tags:
            pinned_ref = f"{tag}@{pins['images'][tag]['digest']}"
            # Match tag optionally followed by @sha256:<64hex>, bounded so ubuntu:26.04
            # never matches inside a longer token or ~ubuntu26.04 filename.
            pattern = re.compile(
                rf"(?:(?<=:-)|(?<![A-Za-z0-9_./:-])){re.escape(tag)}(?:@sha256:[0-9a-f]{{64}})?(?![A-Za-z0-9_./:@-])"
            )
            updated = pattern.sub(pinned_ref, updated)
        out_lines.append(updated)
    return "".join(out_lines)


def apply_pins(root: Path = ROOT, pins: dict | None = None) -> None:
    if pins is None:
        pins = load_pins(root)
    for rel_path in sorted(set(IMAGE_TARGETS) | set(HARNESS_WORKFLOWS)):
        path = root / rel_path
        updated = rewrite_content(rel_path, path.read_text(), pins)
        path.write_text(updated)


def check_repo(root: Path = ROOT) -> list[str]:
    problems: list[str] = []
    try:
        pins = load_pins(root)
    except Exception as exc:
        return [str(exc)]

    target_paths = sorted(set(IMAGE_TARGETS) | set(HARNESS_WORKFLOWS))
    for rel_path in target_paths:
        path = root / rel_path
        if not path.is_file():
            problems.append(f"{rel_path}: missing file")
            continue
        current = path.read_text()
        expected = rewrite_content(rel_path, current, pins)
        if current != expected:
            problems.append(
                f"{rel_path}: image pins out of sync with {PINS_FILE} (run `python3 build-aux/ci/pin_images.py --bump`)"
            )
        for tag in IMAGE_TARGETS.get(rel_path, ()):
            pinned_ref = f"{tag}@{pins['images'][tag]['digest']}"
            if pinned_ref not in current:
                problems.append(f"{rel_path}: missing pinned reference {pinned_ref}")

    expected_hb = harness_bases_value(pins)
    for rel_path in HARNESS_WORKFLOWS:
        path = root / rel_path
        if path.is_file() and f"CASSINI_HARNESS_BASES: {expected_hb}" not in path.read_text():
            problems.append(f"{rel_path}: missing CASSINI_HARNESS_BASES: {expected_hb}")

    # Scan all workflows and Dockerfiles for any unpinned container: or FROM image.
    for wf in sorted((root / ".github/workflows").glob("*.yml")):
        rel = wf.relative_to(root).as_posix()
        if rel in CANARY_EXEMPT_WORKFLOWS:
            continue
        in_container_block_indent: int | None = None
        for lineno, line in enumerate(wf.read_text().splitlines(), 1):
            stripped = line.lstrip()
            if not stripped or stripped.startswith("#"):
                continue
            indent = len(line) - len(stripped)
            if in_container_block_indent is not None and indent <= in_container_block_indent:
                in_container_block_indent = None
            if re.match(r"^container:\s*$", stripped):
                in_container_block_indent = indent
            elif m := re.match(r"^container:\s*(\S+)", stripped):
                ref = m.group(1).strip("'\"")
                if not re.search(r"@sha256:[0-9a-f]{64}$", ref):
                    problems.append(f"{rel}:{lineno}: unpinned workflow container {ref!r}")
            elif in_container_block_indent is not None and (m := re.match(r"^image:\s*(\S+)", stripped)):
                ref = m.group(1).strip("'\"")
                if not re.search(r"@sha256:[0-9a-f]{64}$", ref):
                    problems.append(f"{rel}:{lineno}: unpinned workflow container image {ref!r}")
            if (
                ":latest" in line
                and not stripped.startswith("description:")
                and "@sha256:" not in line
                and "ubuntu-latest" not in line
            ):
                problems.append(f"{rel}:{lineno}: unpinned :latest reference")

    for df in sorted([*root.glob("**/Dockerfile"), *root.glob("**/*.Dockerfile"), *root.glob("**/Containerfile")]):
        rel = df.relative_to(root).as_posix()
        if rel.startswith(".") or rel in HARNESS_STAGE_DOCKERFILES:
            continue
        text = df.read_text()
        for lineno, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if m := re.match(r"^ARG\s+BASE=(\S+)$", stripped):
                if not re.search(r"@sha256:[0-9a-f]{64}$", m.group(1)):
                    problems.append(f"{rel}:{lineno}: unpinned ARG BASE={m.group(1)}")
            elif m := re.match(r"^FROM\s+(\S+)", stripped):
                ref = m.group(1)
                if not ref.startswith("$") and not re.search(r"@sha256:[0-9a-f]{64}$", ref):
                    problems.append(f"{rel}:{lineno}: unpinned FROM {ref}")

    return problems


def bump_repo(root: Path = ROOT, resolver=resolve_image) -> None:
    resolved = {tag: resolver(tag) for tag in IMAGES}
    pins = {
        "updated": dt.datetime.now(dt.UTC).strftime("%Y-%m-%d"),
        "bump_command": "python3 build-aux/ci/pin_images.py --bump",
        "canary_exceptions": sorted(CANARY_EXEMPT_WORKFLOWS),
        "images": resolved,
    }
    pins_path = root / PINS_FILE
    pins_path.parent.mkdir(parents=True, exist_ok=True)
    pins_path.write_text(json.dumps(pins, indent=2, sort_keys=False) + "\n")
    apply_pins(root=root, pins=pins)


def main(argv: list[str] | None = None, root: Path = ROOT) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true", help="Verify all pinned CI images match image-pins.json")
    group.add_argument(
        "--bump",
        action="store_true",
        help="Resolve current OCI digests and update image-pins.json + targets",
    )
    args = parser.parse_args(argv)

    if args.bump:
        bump_repo(root=root)
        problems = check_repo(root=root)
        if problems:
            for p in problems:
                print(f"error: {p}", file=sys.stderr)
            return 1
        print(f"Updated {PINS_FILE} and verified {len(set(IMAGE_TARGETS) | set(HARNESS_WORKFLOWS))} target files.")
        return 0

    problems = check_repo(root=root)
    if problems:
        for p in problems:
            print(f"error: {p}", file=sys.stderr)
        return 1
    print(f"OK: all CI container images pinned to digests in {PINS_FILE}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
