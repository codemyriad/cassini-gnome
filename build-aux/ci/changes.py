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


def select(paths):
    # Workflow/build infrastructure changes must exercise their consumers too.
    if any(path.startswith((".github/", "build-aux/ci/")) for path in paths):
        return dict.fromkeys(AREAS, True)
    return {
        area: area in POLICY["always"]
        or any(fnmatch.fnmatchcase(path, pattern) for path in paths for pattern in POLICY["paths"].get(area, []))
        for area in AREAS
    }


def detect(event, payload, *, git=subprocess.check_output):
    if event != "pull_request":
        return dict.fromkeys(AREAS, True), "Full run outside a PR", []
    try:
        base, head = (payload["pull_request"][key]["sha"] for key in ("base", "head"))
        if not all(isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{40}", sha) for sha in (base, head)):
            raise ValueError("Unknown PR base or head")
        raw = git(["git", "diff", "--name-only", "--no-renames", "-z", base + "..." + head, "--"])
        paths = [os.fsdecode(path) for path in raw.split(b"\0") if path]
        return select(paths), "PR merge-base diff (renames include both paths)", paths
    except Exception as error:
        return dict.fromkeys(AREAS, True), "Detection unavailable; running everything: " + str(error), []


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
    except Exception as error:
        selected, reason, paths = dict.fromkeys(AREAS, True), "Invalid event; running everything: " + str(error), []
    result = dict(areas=selected, reason=reason, paths=paths)
    args.report.write_text(json.dumps(result, indent=2) + "\n")
    if args.output:
        with args.output.open("a") as output:
            for area, enabled in selected.items():
                output.write(f"{area}={str(enabled).lower()}\n")
    print(json.dumps(result, indent=2))
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(summary).open("a") as output:
            output.write(reason + "\n\n" + ", ".join(area for area, enabled in selected.items() if enabled) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
