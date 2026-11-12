#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Select exact, fully green main inputs for a release; never a PR artifact."""

import argparse
import json
import re
import subprocess
from pathlib import Path

WORKFLOWS = ("ci.yml",)
# Scheduled nightly ci.yml runs probe unpinned rolling base images outside merge
# and release gates; releases only accept pinned push / workflow_dispatch runs.
MAIN_EVENTS = ("push", "workflow_dispatch")
FULL_MATRIX_EVENTS = ("workflow_dispatch",)
# flatpak-stable is never shipped (D45.4); it proves main built, validated
# and smoke-tested the stable Flatpak profile, which CI keeps as a check.
PACKAGES = {"deb-0", "deb-1", "rpm-fedora", "rpm-opensuse", "arch", "flatpak-stable"}
BUILD_PRODUCTS = {"ci-core", "ci-boards", "ci-offline"}
# Jobs a release needs to have run, not merely not failed: since H the Meson
# tests are a job of their own beside Build's products (ci.yml callers' names).
REQUIRED_JOBS = ("Build / ", "Meson tests / ")


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
            and r.get("event") in MAIN_EVENTS
            and r.get("head_branch") == "main"
        ]
        if not matches:
            raise ValueError(f"No main run of {workflow} for {commit}; finish main checks before tagging")
        latest = max(matches, key=lambda r: r["id"])
        if latest.get("status") != "completed" or latest.get("conclusion") != "success":
            raise ValueError(f"Main {workflow} is not green for {commit}: {latest.get('html_url')}")
        full_matches = [
            r
            for r in matches
            if r.get("event") in FULL_MATRIX_EVENTS
            and r.get("status") == "completed"
            and r.get("conclusion") == "success"
        ]
        chosen[workflow] = max(full_matches, key=lambda r: r["id"]) if full_matches else latest
    return chosen


def validate_jobs(jobs, url):
    # A continue-on-error or skipped matrix job must not silently supply release inputs.
    if not jobs or any(j.get("conclusion") != "success" for j in jobs):
        raise ValueError(
            f"Every main job must succeed before release (run full main CI via 'gh workflow run ci.yml --ref main' if only the two-distro push gate ran): {url}"
        )
    for prefix in REQUIRED_JOBS:
        if not any(j.get("name", "").startswith(prefix) for j in jobs):
            raise ValueError(f"No {prefix.removesuffix(' / ')} job in this main run: {url}")


def validate_artifacts(artifacts):
    for name in PACKAGES | BUILD_PRODUCTS:
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
    runs = pages(args.repo, f"actions/runs?head_sha={args.commit}&branch=main", "workflow_runs")
    chosen = select_runs(runs, args.commit)
    for run in chosen.values():
        validate_jobs(pages(args.repo, f"actions/runs/{run['id']}/jobs?filter=latest", "jobs"), run["html_url"])
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
