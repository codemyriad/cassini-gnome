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
# Non-two-distro rolling/packaging legs that ci.yml skips on a main push (#387).
# Unattended daily test releases build their own .deb/.rpm/Arch packages and do
# not ship Flatpak; stable tags still require validate_jobs (every job green).
TEST_RELEASE_SKIPPABLE_JOBS = frozenset(
    {
        "Flatpak",
        "Flatpak / Flatpak",
        "RPM and Arch packages / Build Arch package",
    }
)
TEST_RELEASE_SKIPPABLE_PREFIXES = (
    "Debian package / Build .deb (debian:testing",
    "RPM and Arch packages / Build .rpm (registry.opensuse.org/opensuse/tumbleweed",
    "Source / Build and test on debian:testing",
    "Source / Build and test on registry.opensuse.org/opensuse/tumbleweed",
    "Source / Build and test on archlinux:",
    "Source / Build and test on fedora:latest",
    "Source / Build and test on alpine:edge",
)
MAX_PAGES = 50
DESKTOP_AREAS = ("recording", "boards", "offline", "desktop")
DESKTOP_JOB_AREAS = {
    "Recording": "recording",
    "Recording / capture": "recording",
    "Recording / live-folder": "recording",
    "Boards": "boards",
    "Boards / current-window": "boards",
    "Offline": "offline",
    "Offline / pinned-offline-recognition": "offline",
    "Desktop": "desktop",
    "Desktop / GNOME 50 / installed deb": "desktop",
    "Desktop / GNOME 50 / installed deb and Flatpak": "desktop",
}
_PR_AREA_CALLERS = {
    "recording": ("Recording", frozenset({"Recording / capture", "Recording / live-folder"})),
    "boards": ("Boards", frozenset({"Boards / current-window"})),
    "offline": ("Offline", frozenset({"Offline / pinned-offline-recognition"})),
    "desktop": (
        "Desktop",
        frozenset({"Desktop / GNOME 50 / installed deb", "Desktop / GNOME 50 / installed deb and Flatpak"}),
    ),
}


def api(repo, path):
    return json.loads(subprocess.check_output(["gh", "api", f"repos/{repo}/{path}"], timeout=60))


def desktop_area_for_job(name):
    return DESKTOP_JOB_AREAS.get(name)


def green_desktop_areas_in_pr_jobs(jobs):
    if not jobs:
        return frozenset()
    for job in jobs:
        if job.get("status", "completed") != "completed" or job.get("conclusion") not in ("success", "skipped"):
            return frozenset()
    for prefix in REQUIRED_JOBS:
        if not any(
            j.get("name", "").startswith(prefix)
            and j.get("status", "completed") == "completed"
            and j.get("conclusion") == "success"
            for j in jobs
        ):
            return frozenset()
    verified = set()
    for area, (caller, expected) in _PR_AREA_CALLERS.items():
        area_jobs = [j for j in jobs if j.get("name", "") == caller or j.get("name", "").startswith(f"{caller} / ")]
        if not area_jobs or any(
            j.get("status", "completed") != "completed" or j.get("conclusion") != "success" for j in area_jobs
        ):
            continue
        names = {j.get("name", "") for j in area_jobs}
        if area == "desktop":
            if not (names & expected):
                continue
            verified.add("desktop")
            if "Desktop / GNOME 50 / installed deb and Flatpak" in names:
                verified.add("desktop_flatpak")
        elif expected <= names:
            verified.add(area)
    return frozenset(verified)


def _is_noop_skipped_pr_run(run, jobs):
    if run.get("status") != "completed":
        return False
    if run.get("conclusion") == "skipped":
        return True
    if run.get("conclusion") in ("success", "neutral") and jobs:
        return all(j.get("status", "completed") == "completed" and j.get("conclusion") == "skipped" for j in jobs)
    return False


def verified_pr_desktop_areas_for_pr_head(repo, pr_sha, *, list_runs=None, list_jobs=None):
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo) or not re.fullmatch(r"[0-9a-f]{40}", pr_sha):
        return frozenset()
    fetch_runs = list_runs or (
        lambda sha: pages(repo, f"actions/workflows/ci.yml/runs?event=pull_request&head_sha={sha}", "workflow_runs")
    )
    fetch_jobs = list_jobs or (lambda run_id: pages(repo, f"actions/runs/{run_id}/jobs?filter=latest", "jobs"))
    matches = [
        r
        for r in fetch_runs(pr_sha)
        if r.get("event") == "pull_request"
        and r.get("head_sha") == pr_sha
        and (r.get("head_repository") or {}).get("full_name") == repo
        and r.get("path") in (None, ".github/workflows/ci.yml")
    ]
    for run in sorted(matches, key=lambda r: r["id"], reverse=True):
        if run.get("status") == "completed" and run.get("conclusion") == "skipped":
            continue
        if run.get("status") != "completed":
            return frozenset()
        jobs = list(fetch_jobs(run["id"]))
        if _is_noop_skipped_pr_run(run, jobs):
            continue
        if run.get("conclusion") != "success":
            return frozenset()
        return green_desktop_areas_in_pr_jobs(jobs)
    return frozenset()


def verified_pr_desktop_areas(repo, commit, *, get_commit=None, list_runs=None, list_jobs=None):
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo) or not re.fullmatch(r"[0-9a-f]{40}", commit):
        return frozenset()
    fetch_commit = get_commit or (lambda sha: api(repo, f"commits/{sha}"))
    merge_data = fetch_commit(commit)
    parents = merge_data.get("parents") if isinstance(merge_data, dict) else None
    if not isinstance(parents, list) or len(parents) != 2:
        return frozenset()
    pr_sha = parents[1].get("sha", "") if isinstance(parents[1], dict) else ""
    if not re.fullmatch(r"[0-9a-f]{40}", pr_sha):
        return frozenset()
    merge_tree = (((merge_data.get("commit") or {}).get("tree") or {}).get("sha")) or ""
    if not re.fullmatch(r"[0-9a-f]{40}", merge_tree):
        return frozenset()
    pr_data = fetch_commit(pr_sha)
    pr_tree = (
        (((pr_data if isinstance(pr_data, dict) else {}).get("commit") or {}).get("tree") or {}).get("sha")
    ) or ""
    if merge_tree != pr_tree:
        return frozenset()
    return verified_pr_desktop_areas_for_pr_head(repo, pr_sha, list_runs=list_runs, list_jobs=list_jobs)


def _desktop_skip_verified(name, verified_desktop_areas, *, require_flatpak_desktop=False):
    area = desktop_area_for_job(name)
    if area is None or area not in verified_desktop_areas:
        return False
    return not (area == "desktop" and require_flatpak_desktop and "desktop_flatpak" not in verified_desktop_areas)


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


def validate_jobs(jobs, url, *, verified_desktop_areas=frozenset()):
    # A continue-on-error or skipped matrix job must not silently supply release inputs;
    # only identical-tree desktop jobs already proven green in the merged PR may skip on push.
    if not jobs:
        raise ValueError(
            f"Every main job must succeed before release (run full main CI via 'gh workflow run ci.yml --ref main' if only the two-distro push gate ran): {url}"
        )
    flatpak_ran = any(
        (j.get("name") == "Flatpak" or j.get("name", "").startswith("Flatpak / ")) and j.get("conclusion") == "success"
        for j in jobs
    )
    for job in jobs:
        status = job.get("status", "completed")
        conclusion = job.get("conclusion")
        if status == "completed" and conclusion == "success":
            continue
        if (
            status == "completed"
            and conclusion == "skipped"
            and _desktop_skip_verified(job.get("name", ""), verified_desktop_areas, require_flatpak_desktop=flatpak_ran)
        ):
            continue
        raise ValueError(
            f"Every main job must succeed before release (run full main CI via 'gh workflow run ci.yml --ref main' if only the two-distro push gate ran): {url}"
        )
    for prefix in REQUIRED_JOBS:
        if not any(j.get("name", "").startswith(prefix) and j.get("conclusion") == "success" for j in jobs):
            raise ValueError(f"No {prefix.removesuffix(' / ')} job in this main run: {url}")


def _skippable_for_test_release(name):
    return name in TEST_RELEASE_SKIPPABLE_JOBS or name.startswith(TEST_RELEASE_SKIPPABLE_PREFIXES)


def validate_test_release_jobs(jobs, url, *, verified_desktop_areas=frozenset()):
    if not jobs:
        raise ValueError(f"No main jobs recorded for test release: {url}")
    for job in jobs:
        if job.get("status", "completed") != "completed":
            raise ValueError(f"Main job is not completed ({job.get('name')}): {url}")
        conclusion = job.get("conclusion")
        if conclusion == "success":
            continue
        name = job.get("name", "")
        if conclusion == "skipped" and (
            _skippable_for_test_release(name) or _desktop_skip_verified(name, verified_desktop_areas)
        ):
            continue
        raise ValueError(f"Main gate job did not succeed ({name}: {conclusion}): {url}")
    for prefix in REQUIRED_JOBS:
        if not any(j.get("name", "").startswith(prefix) and j.get("conclusion") == "success" for j in jobs):
            raise ValueError(f"No green {prefix.removesuffix(' / ')} job in this main run: {url}")


def validate_artifacts(artifacts):
    for name in PACKAGES | BUILD_PRODUCTS:
        matches = [a for a in artifacts if a.get("name") == name and not a.get("expired")]
        if len(matches) != 1:
            raise ValueError(f"Expected one unexpired {name} artifact from this exact main run")


def pages(repo, path, key):
    values = []
    separator = "&" if "?" in path else "?"
    for page in range(1, MAX_PAGES + 1):
        response = api(repo, f"{path}{separator}per_page=100&page={page}")
        values.extend(response[key])
        if len(response[key]) < 100:
            return values
    raise ValueError(f"Pagination limit exceeded for {path}")


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
        jobs = pages(args.repo, f"actions/runs/{run['id']}/jobs?filter=latest", "jobs")
        verified = frozenset()
        if run.get("event") == "push" and any(
            j.get("conclusion") == "skipped" and desktop_area_for_job(j.get("name", "")) is not None for j in jobs
        ):
            verified = verified_pr_desktop_areas(args.repo, args.commit)
        validate_jobs(jobs, run["html_url"], verified_desktop_areas=verified)
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
