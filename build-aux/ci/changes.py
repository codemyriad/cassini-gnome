#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Preserve the pre-orchestrator PR coverage; unknown history always runs all areas."""

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
from pathlib import Path

_CI_DIR = str(Path(__file__).resolve().parent)
if _CI_DIR not in sys.path:
    sys.path.insert(0, _CI_DIR)
import green_main  # noqa: E402

POLICY = json.loads(Path(__file__).with_name("pr-paths.json").read_text())
AREAS = (*POLICY["always"], *POLICY["paths"])
LEGS = tuple(POLICY["package_legs"])
PUSH_SKIPPABLE_AREAS = ("recording", "boards", "offline")


def select(paths):
    # Workflow/build infrastructure changes must exercise their consumers too.
    if any(path.startswith((".github/", "build-aux/ci/")) for path in paths):
        return dict.fromkeys(AREAS, True)
    return {
        area: area in POLICY["always"]
        or any(fnmatch.fnmatchcase(path, pattern) for path in paths for pattern in POLICY["paths"].get(area, []))
        for area in AREAS
    }


def select_legs(paths):
    # Workflow/build infrastructure changes must exercise all package/distro legs too.
    if any(path.startswith((".github/", "build-aux/ci/")) for path in paths):
        return dict.fromkeys(LEGS, True)
    return {
        leg: any(fnmatch.fnmatchcase(path, pattern) for path in paths for pattern in POLICY["package_legs"][leg])
        for leg in LEGS
    }


def extract_pr_labels(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get("pull_request"), dict):
        raise ValueError("Missing pull_request object")
    raw = payload["pull_request"].get("labels")
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(
        isinstance(item, dict) and isinstance(item.get("name"), str) for item in raw
    ):
        raise ValueError("Invalid pull_request.labels")
    return tuple(item["name"] for item in raw)


def execution_tier(event, payload):
    if event == "push":
        return False
    if event != "pull_request":
        return True
    try:
        labels = extract_pr_labels(payload)
        return any(label.casefold() == "full-ci" for label in labels)
    except Exception:
        return True


def detect(event, payload, *, git=subprocess.check_output):
    if event != "pull_request":
        return dict.fromkeys(AREAS, True), "Full run outside a PR", []
    try:
        labels = extract_pr_labels(payload)
        if any(label.casefold() == "full-ci" for label in labels):
            return dict.fromkeys(AREAS, True), "PR labeled full-ci; running everything", []
        base, head = (payload["pull_request"][key]["sha"] for key in ("base", "head"))
        if not all(isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{40}", sha) for sha in (base, head)):
            raise ValueError("Unknown PR base or head")
        raw = git(["git", "diff", "--name-only", "--no-renames", "-z", base + "..." + head, "--"])
        paths = [os.fsdecode(path) for path in raw.split(b"\0") if path]
        return select(paths), "PR merge-base diff (renames include both paths)", paths
    except Exception as error:
        return dict.fromkeys(AREAS, True), "Detection unavailable; running everything: " + str(error), []


def resolve_package_legs(event, *, full_ci, reason, paths):
    if full_ci or reason.startswith("Detection unavailable;"):
        return dict.fromkeys(LEGS, True)
    if event == "pull_request":
        return select_legs(paths)
    return dict.fromkeys(LEGS, False)


def apply_push_pr_tree_skips(
    event,
    selected,
    *,
    repo="",
    ref="refs/heads/main",
    commit="HEAD",
    flatpak_enabled=False,
    git=subprocess.check_output,
    verifier=None,
):
    areas = dict(selected)
    if event != "push" or not repo or (ref and ref != "refs/heads/main"):
        return areas, True, None
    try:
        revs = git(["git", "rev-list", "--parents", "-n", "1", commit]).decode().strip().split()
        if len(revs) != 3 or not all(re.fullmatch(r"[0-9a-f]{40}", sha) for sha in revs):
            return areas, True, None
        head_sha, _first_parent, pr_sha = revs
        head_tree = git(["git", "rev-parse", f"{head_sha}^{{tree}}"]).decode().strip()
        pr_tree = git(["git", "rev-parse", f"{pr_sha}^{{tree}}"]).decode().strip()
        if not re.fullmatch(r"[0-9a-f]{40}", head_tree) or head_tree != pr_tree:
            return areas, True, f"Push merge tree differs from PR {pr_sha[:12]}; running desktop jobs"
        check_pr = verifier or green_main.verified_pr_desktop_areas_for_pr_head
        verified = check_pr(repo, pr_sha)
        skipped = []
        for area in PUSH_SKIPPABLE_AREAS:
            if area in verified:
                areas[area] = False
                skipped.append(area)
        desktop_key = "desktop_flatpak" if flatpak_enabled else "desktop"
        desktop = desktop_key not in verified
        if not desktop:
            skipped.append("desktop")
        if not skipped:
            return areas, True, f"Push tree matches PR {pr_sha[:12]}, no verified green PR desktop areas"
        return areas, desktop, f"Push tree matches green PR {pr_sha[:12]}; skipping {', '.join(skipped)}"
    except Exception as error:
        return dict(selected), True, f"Push PR tree check unavailable; running desktop jobs: {error}"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event", default=os.environ.get("GITHUB_EVENT_NAME", "workflow_dispatch"))
    parser.add_argument("--payload", type=Path, default=os.environ.get("GITHUB_EVENT_PATH"))
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--ref", default=os.environ.get("GITHUB_REF", "refs/heads/main"))
    parser.add_argument("--commit", default="HEAD")
    parser.add_argument("--output", type=Path, default=os.environ.get("GITHUB_OUTPUT"))
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    try:
        payload = json.loads(args.payload.read_text()) if args.payload else {}
        selected, reason, paths = detect(args.event, payload)
        full_ci = True if reason.startswith("Detection unavailable;") else execution_tier(args.event, payload)
        legs = resolve_package_legs(args.event, full_ci=full_ci, reason=reason, paths=paths)
        selected, desktop, skip_note = apply_push_pr_tree_skips(
            args.event,
            selected,
            repo=args.repo,
            ref=args.ref,
            commit=args.commit,
            flatpak_enabled=legs.get("flatpak", False),
        )
        if skip_note:
            reason = f"{reason} ({skip_note})"
    except Exception as error:
        selected, reason, paths = dict.fromkeys(AREAS, True), "Invalid event; running everything: " + str(error), []
        desktop = True
        full_ci = True
        legs = dict.fromkeys(LEGS, True)
    result = dict(areas=selected, desktop=desktop, package_legs=legs, full_ci=full_ci, reason=reason, paths=paths)
    args.report.write_text(json.dumps(result, indent=2) + "\n")
    if args.output:
        with args.output.open("a") as output:
            for area, enabled in selected.items():
                output.write(f"{area}={str(enabled).lower()}\n")
            output.write(f"desktop={str(desktop).lower()}\n")
            for leg, enabled in legs.items():
                output.write(f"{leg}={str(enabled).lower()}\n")
            output.write(f"full_ci={str(full_ci).lower()}\n")
    print(json.dumps(result, indent=2))
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(summary).open("a") as output:
            enabled_list = [area for area, enabled in selected.items() if enabled]
            if desktop:
                enabled_list.append("desktop")
            enabled_areas = ", ".join(enabled_list)
            enabled_legs = ", ".join(leg for leg, enabled in legs.items() if enabled) or "two-distro default"
            output.write(f"{reason} (full_ci={str(full_ci).lower()}, extra_legs={enabled_legs})\n\n{enabled_areas}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
