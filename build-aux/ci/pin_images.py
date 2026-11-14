#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Pin CI container images by @sha256 digest and verify zero floating gate references.

    python3 build-aux/ci/pin_images.py --check   # verify repo files match build-aux/ci/image-pins.json
    python3 build-aux/ci/pin_images.py --bump    # query OCI registries and rewrite pins in one step

Nightly rolling-image coverage (outside merge and release gates):
- PR, main push, full-ci, workflow_dispatch, and daily-test-release legs use @sha256 digests.
- Scheduled nightly ci.yml runs (github.event_name == 'schedule') probe unpinned rolling tags
  (debian:testing, registry.opensuse.org/opensuse/tumbleweed:latest, archlinux:latest,
  alpine:edge, fedora:latest) alongside .github/workflows/rpm-canary.yml so a red nightly
  rolling leg signals an upstream image change to validate and pin via --bump.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
PINS_FILE = Path("build-aux/ci/image-pins.json")

FIXED_IMAGES = (
    "ubuntu:26.04",
    "fedora:44",
)

ROLLING_IMAGES = (
    "debian:testing",
    "fedora:latest",
    "registry.opensuse.org/opensuse/tumbleweed:latest",
    "archlinux:latest",
    "alpine:edge",
)

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
    # The prebuilt-image publish matrix must list the same digests deb/packages/source build from.
    ".github/workflows/ci-images.yml": (
        "ubuntu:26.04",
        "debian:testing",
        "fedora:44",
        "fedora:latest",
        "registry.opensuse.org/opensuse/tumbleweed:latest",
        "archlinux:latest",
    ),
    ".github/workflows/daily-test-release.yml": (
        "ubuntu:26.04",
        "debian:testing",
        "fedora:44",
        "registry.opensuse.org/opensuse/tumbleweed:latest",
    ),
    ".github/workflows/meson-tests.yml": ("ubuntu:26.04",),
    ".github/workflows/source.yml": IMAGES,
    ".github/workflows/deb.yml": ("ubuntu:26.04", "debian:testing"),
    ".github/workflows/packages.yml": (
        "fedora:44",
        "registry.opensuse.org/opensuse/tumbleweed:latest",
        "archlinux:latest",
    ),
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

# Reusable CI workflows whose nightly schedule branch probes unpinned rolling images.
SCHEDULE_ROLLING_TARGETS: dict[str, tuple[str, ...]] = {
    ".github/workflows/source.yml": ROLLING_IMAGES,
    ".github/workflows/deb.yml": ("debian:testing",),
    ".github/workflows/packages.yml": (
        "registry.opensuse.org/opensuse/tumbleweed:latest",
        "archlinux:latest",
    ),
}

RELEASE_GATE_SCRIPTS = (
    "build-aux/ci/green_main.py",
    "build-aux/ci/daily_test_release.py",
)

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
    schedule_rolling = set(SCHEDULE_ROLLING_TARGETS.get(rel_path, ()))
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
        is_schedule_line = bool(schedule_rolling) and "github.event_name == 'schedule'" in line
        updated = line
        for tag in tags:
            pinned_ref = f"{tag}@{pins['images'][tag]['digest']}"
            if is_schedule_line and tag in schedule_rolling:
                # Preserve bare unpinned rolling tags on the schedule branch while refreshing @sha256 digests.
                pattern = re.compile(
                    rf"(?:(?<=:-)|(?<![A-Za-z0-9_./:-])){re.escape(tag)}@sha256:[0-9a-f]{{64}}(?![A-Za-z0-9_./:@-])"
                )
            else:
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


def _eval_gh_expr(expr: str, *, event_name: str, inputs: dict[str, bool]):
    body = expr.strip()
    if body.startswith("${{") and body.endswith("}}"):
        body = body[3:-2].strip()

    def _replace_ops(match: re.Match[str]) -> str:
        if match.group(1) is not None:
            return match.group(1)
        return " and " if match.group(2) is not None else " or "

    py_expr = re.sub(r"('(?:''|[^'])*')|(&&)|(\|\|)", _replace_ops, body)
    env = {
        "github": SimpleNamespace(event_name=event_name),
        "inputs": SimpleNamespace(**inputs),
        "fromJSON": json.loads,
        "format": lambda fmt, *args: fmt.format(*args),
    }
    return eval(py_expr, {"__builtins__": {}}, env)


def _check_rolling_nightly_wiring(root: Path, pins: dict) -> list[str]:
    problems: list[str] = []

    def pinned(tag: str) -> str:
        return f"{tag}@{pins['images'][tag]['digest']}"

    source_path = root / ".github/workflows/source.yml"
    deb_path = root / ".github/workflows/deb.yml"
    packages_path = root / ".github/workflows/packages.yml"
    if not (source_path.is_file() and deb_path.is_file() and packages_path.is_file()):
        return problems

    source_m = re.search(r"^        include: (\$\{\{.*\}\})$", source_path.read_text(), re.M)
    deb_m = re.search(r"^        base: (\$\{\{.*\}\})$", deb_path.read_text(), re.M)
    rpm_m = re.search(r"^        include: (\$\{\{.*\}\})$", packages_path.read_text(), re.M)
    arch_m = re.search(r'^      - run: build-aux/arch/build\.sh "(\$\{\{.*\}\})"$', packages_path.read_text(), re.M)
    if not source_m:
        problems.append(".github/workflows/source.yml: missing distros matrix include expression")
    if not deb_m:
        problems.append(".github/workflows/deb.yml: missing build matrix base expression")
    if not rpm_m:
        problems.append(".github/workflows/packages.yml: missing rpm matrix include expression")
    if not arch_m:
        problems.append(
            ".github/workflows/packages.yml: arch job must pass schedule/pinned image to build-aux/arch/build.sh"
        )
    if problems:
        return problems

    all_true = {"extra_distros": True, "deb_testing": True, "rpm_opensuse": True, "arch": True}
    all_false = {"extra_distros": False, "deb_testing": False, "rpm_opensuse": False, "arch": False}

    try:
        # 1. Nightly schedule runs probe all 5 rolling images UNPINNED while keeping fixed releases pinned.
        sched_source = _eval_gh_expr(source_m.group(1), event_name="schedule", inputs=all_true)
        expected_sched_source = [
            {
                "image": pinned("ubuntu:26.04"),
                "pytest-in": f"Debian package / Build .deb ({pinned('ubuntu:26.04')})",
            },
            {"image": "fedora:latest"},
            {"image": "alpine:edge"},
            {
                "image": "debian:testing",
                "pytest-in": "Debian package / Build .deb (debian:testing)",
            },
            {
                "image": "registry.opensuse.org/opensuse/tumbleweed:latest",
                "pytest-in": "RPM and Arch packages / Build .rpm (registry.opensuse.org/opensuse/tumbleweed:latest)",
            },
            {
                "image": "archlinux:latest",
                "pytest-in": "RPM and Arch packages / Build Arch package",
            },
        ]
        if sched_source != expected_sched_source:
            problems.append(
                ".github/workflows/source.yml: schedule matrix must keep ubuntu:26.04 pinned and run rolling legs unpinned"
            )

        sched_deb = _eval_gh_expr(deb_m.group(1), event_name="schedule", inputs=all_true)
        if sched_deb != [pinned("ubuntu:26.04"), "debian:testing"]:
            problems.append(
                ".github/workflows/deb.yml: schedule matrix must keep ubuntu:26.04 pinned and run debian:testing unpinned"
            )

        sched_rpm = _eval_gh_expr(rpm_m.group(1), event_name="schedule", inputs=all_true)
        expected_sched_rpm = [
            {"base": pinned("fedora:44"), "name": "fedora"},
            {"base": "registry.opensuse.org/opensuse/tumbleweed:latest", "name": "opensuse"},
        ]
        if sched_rpm != expected_sched_rpm:
            problems.append(
                ".github/workflows/packages.yml: schedule rpm matrix must keep fedora:44 pinned and run tumbleweed unpinned"
            )

        sched_arch = _eval_gh_expr(arch_m.group(1), event_name="schedule", inputs=all_true)
        if sched_arch != "archlinux:latest":
            problems.append(".github/workflows/packages.yml: schedule arch build must run archlinux:latest unpinned")

        # 2. PR, main push, full-ci, and workflow_dispatch keep EVERY leg @sha256-pinned.
        expected_pinned_source_full = [
            {
                "image": pinned("ubuntu:26.04"),
                "pytest-in": f"Debian package / Build .deb ({pinned('ubuntu:26.04')})",
            },
            {"image": pinned("fedora:latest")},
            {"image": pinned("alpine:edge")},
            {
                "image": pinned("debian:testing"),
                "pytest-in": f"Debian package / Build .deb ({pinned('debian:testing')})",
            },
            {
                "image": pinned("registry.opensuse.org/opensuse/tumbleweed:latest"),
                "pytest-in": (
                    f"RPM and Arch packages / Build .rpm ({pinned('registry.opensuse.org/opensuse/tumbleweed:latest')})"
                ),
            },
            {
                "image": pinned("archlinux:latest"),
                "pytest-in": "RPM and Arch packages / Build Arch package",
            },
        ]
        expected_pinned_source_pr = [
            {
                "image": pinned("ubuntu:26.04"),
                "pytest-in": f"Debian package / Build .deb ({pinned('ubuntu:26.04')})",
            },
            {"image": pinned("fedora:44")},
        ]
        for ev in ("pull_request", "push", "workflow_dispatch"):
            if _eval_gh_expr(source_m.group(1), event_name=ev, inputs=all_true) != expected_pinned_source_full:
                problems.append(f".github/workflows/source.yml: {ev} full matrix must pin all 6 images")
            if _eval_gh_expr(source_m.group(1), event_name=ev, inputs=all_false) != expected_pinned_source_pr:
                problems.append(
                    f".github/workflows/source.yml: {ev} two-distro matrix must pin ubuntu:26.04 + fedora:44"
                )
            if _eval_gh_expr(deb_m.group(1), event_name=ev, inputs=all_true) != [
                pinned("ubuntu:26.04"),
                pinned("debian:testing"),
            ]:
                problems.append(f".github/workflows/deb.yml: {ev} full matrix must pin ubuntu:26.04 + debian:testing")
            if _eval_gh_expr(deb_m.group(1), event_name=ev, inputs=all_false) != [pinned("ubuntu:26.04")]:
                problems.append(f".github/workflows/deb.yml: {ev} two-distro matrix must pin ubuntu:26.04")
            if _eval_gh_expr(rpm_m.group(1), event_name=ev, inputs=all_true) != [
                {"base": pinned("fedora:44"), "name": "fedora"},
                {"base": pinned("registry.opensuse.org/opensuse/tumbleweed:latest"), "name": "opensuse"},
            ]:
                problems.append(f".github/workflows/packages.yml: {ev} full rpm matrix must pin fedora:44 + tumbleweed")
            if _eval_gh_expr(rpm_m.group(1), event_name=ev, inputs=all_false) != [
                {"base": pinned("fedora:44"), "name": "fedora"}
            ]:
                problems.append(f".github/workflows/packages.yml: {ev} two-distro rpm matrix must pin fedora:44")
            if _eval_gh_expr(arch_m.group(1), event_name=ev, inputs=all_true) != pinned("archlinux:latest"):
                problems.append(f".github/workflows/packages.yml: {ev} arch build must pin archlinux:latest")
    except Exception as exc:
        problems.append(f"failed to evaluate rolling/pinned workflow expressions: {exc}")

    # 3. Release gates must NOT admit scheduled nightly runs (unpinned rolling legs stay outside release gates).
    green_main_path = root / "build-aux/ci/green_main.py"
    if green_main_path.is_file():
        spec = importlib.util.spec_from_file_location("_check_green_main", green_main_path)
        gm = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gm)
        if "schedule" in gm.MAIN_EVENTS or "schedule" in gm.FULL_MATRIX_EVENTS:
            problems.append("build-aux/ci/green_main.py: schedule runs must stay outside release gates")

    daily_path = root / "build-aux/ci/daily_test_release.py"
    if daily_path.is_file():
        daily_text = daily_path.read_text()
        m_events = re.search(r"^TRUSTED_CI_EVENTS\s*=\s*\{([^}]*)\}", daily_text, re.M)
        if not m_events or re.search(r"['\"]schedule['\"]", m_events.group(1)):
            problems.append("build-aux/ci/daily_test_release.py: schedule runs must stay outside daily release gate")

    return problems


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

    problems.extend(_check_rolling_nightly_wiring(root, pins))

    # Scan all workflows and Dockerfiles for any unpinned container:, FROM, or stray :latest image.
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
            is_allowed_schedule_line = rel in SCHEDULE_ROLLING_TARGETS and "github.event_name == 'schedule'" in line
            if (
                not is_allowed_schedule_line
                and not stripped.startswith("description:")
                and "ubuntu-latest" not in line
                and re.search(r":latest(?![A-Za-z0-9_./:-]|@sha256:[0-9a-f]{64})", line)
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
        "nightly_unpinned_rolling": list(ROLLING_IMAGES),
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
    print(f"OK: all CI container images pinned to digests in {PINS_FILE} (rolling legs unpinned on nightly schedule).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
