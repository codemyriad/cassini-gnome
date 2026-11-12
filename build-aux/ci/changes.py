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

POLICY = json.loads(Path(__file__).with_name("pr-paths.json").read_text())
AREAS = (*POLICY["always"], *POLICY["paths"])
LEGS = tuple(POLICY["package_legs"])


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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event", default=os.environ.get("GITHUB_EVENT_NAME", "workflow_dispatch"))
    parser.add_argument("--payload", type=Path, default=os.environ.get("GITHUB_EVENT_PATH"))
    parser.add_argument("--output", type=Path, default=os.environ.get("GITHUB_OUTPUT"))
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    try:
        payload = json.loads(args.payload.read_text()) if args.payload else {}
        selected, reason, paths = detect(args.event, payload)
        full_ci = True if reason.startswith("Detection unavailable;") else execution_tier(args.event, payload)
        legs = resolve_package_legs(args.event, full_ci=full_ci, reason=reason, paths=paths)
    except Exception as error:
        selected, reason, paths = dict.fromkeys(AREAS, True), "Invalid event; running everything: " + str(error), []
        full_ci = True
        legs = dict.fromkeys(LEGS, True)
    result = dict(areas=selected, package_legs=legs, full_ci=full_ci, reason=reason, paths=paths)
    args.report.write_text(json.dumps(result, indent=2) + "\n")
    if args.output:
        with args.output.open("a") as output:
            for area, enabled in selected.items():
                output.write(f"{area}={str(enabled).lower()}\n")
            for leg, enabled in legs.items():
                output.write(f"{leg}={str(enabled).lower()}\n")
            output.write(f"full_ci={str(full_ci).lower()}\n")
    print(json.dumps(result, indent=2))
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(summary).open("a") as output:
            enabled_areas = ", ".join(area for area, enabled in selected.items() if enabled)
            enabled_legs = ", ".join(leg for leg, enabled in legs.items() if enabled) or "two-distro default"
            output.write(f"{reason} (full_ci={str(full_ci).lower()}, extra_legs={enabled_legs})\n\n{enabled_areas}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
