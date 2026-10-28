#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Select exact, fully green main inputs for a release; never a PR artifact."""

import argparse
import json
import re
import subprocess
from pathlib import Path

WORKFLOWS = ("ci.yml", "recording.yml", "ux-shot.yml", "ux-library.yml", "whistle.yml")
PACKAGES = {"deb-0", "deb-1", "rpm-fedora", "rpm-opensuse", "arch"}


def api(repo, path):
    return json.loads(subprocess.check_output(["gh", "api", f"repos/{repo}/{path}"]))


def select_runs(runs, commit):
    chosen = {}
    for workflow in WORKFLOWS:
        matches = [
            r
            for r in runs
            if r.get("path") == ".github/workflows/" + workflow
            and r.get("head_sha") == commit
            and r.get("event") == "push"
            and r.get("head_branch") == "main"
        ]
        if not matches:
            raise ValueError(f"No main push run of {workflow} for {commit}; finish main checks before tagging")
        run = max(matches, key=lambda r: r["id"])
        if run.get("status") != "completed" or run.get("conclusion") != "success":
            raise ValueError(f"Main {workflow} is not green for {commit}: {run.get('html_url')}")
        chosen[workflow] = run
    return chosen


def validate_artifacts(artifacts):
    for name in PACKAGES:
        matches = [a for a in artifacts if a.get("name") == name and not a.get("expired")]
        if len(matches) != 1:
            raise ValueError(f"Expected one unexpired {name} artifact from this exact main run")


def pages(repo, path, key):
    values, page = [], 1
    while True:
        separator = "&" if "?" in path else "?"
        response = api(repo, f"{path}{separator}per_page=100&page={page}")
        values.extend(response[key])
        if len(response[key]) < 100:
            return values
        page += 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default="codemyriad/cassini-gnome")
    parser.add_argument("--commit", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not re.fullmatch("[0-9a-f]{40}", args.commit):
        parser.error("An exact commit SHA is required")
    runs = pages(args.repo, f"actions/runs?head_sha={args.commit}&event=push&branch=main", "workflow_runs")
    chosen = select_runs(runs, args.commit)
    # A continue-on-error matrix job must not silently supply release inputs.
    for run in chosen.values():
        jobs = pages(args.repo, f"actions/runs/{run['id']}/jobs?filter=latest", "jobs")
        if not jobs or any(j.get("conclusion") != "success" for j in jobs):
            raise ValueError(f"Every main job must succeed before release: {run['html_url']}")
    ci = chosen["ci.yml"]
    artifacts = pages(args.repo, f"actions/runs/{ci['id']}/artifacts", "artifacts")
    validate_artifacts(artifacts)
    with args.output.open("a") as output:
        output.write(f"run_id={ci['id']}\n")
    print(
        json.dumps(
            dict(
                commit=args.commit,
                mainRuns={name: run["id"] for name, run in chosen.items()},
                packageArtifacts=sorted(PACKAGES),
            ),
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
