# SPDX-License-Identifier: GPL-3.0-or-later
"""G13/G14: every job has a time budget, and CI runs on pushes to main only."""

import re
from pathlib import Path

WORKFLOWS = Path(__file__).parents[1] / ".github/workflows"


def jobs(workflow):
    body = workflow.read_text().split("\njobs:\n", 1)[1]
    for block in re.split(r"\n(?=  [A-Za-z0-9_-]+:\n)", "\n" + body):
        if match := re.match(r"\s*([A-Za-z0-9_-]+):\n", block):
            yield match.group(1), block


def test_every_job_has_a_timeout():
    # GitHub's default is 360 min: a hung job holds a runner for six hours.
    # A reusable-workflow call takes the called jobs' own budgets.
    missing = [
        f"{workflow.name}:{name}"
        for workflow in sorted(WORKFLOWS.glob("*.yml"))
        if "\njobs:\n" in workflow.read_text()
        for name, block in jobs(workflow)
        if not re.search(r"^    uses: ", block, re.M) and not re.search(r"^    timeout-minutes: \d+", block, re.M)
    ]
    assert missing == []


def test_ci_runs_on_pushes_to_main_only():
    trigger = re.search(r"^  push:\n    branches: (.+)$", (WORKFLOWS / "ci.yml").read_text(), re.M)
    assert trigger and trigger.group(1) == "[main]"


def test_release_metadata_runs_in_lint_and_release_preparation_has_no_pr_trigger():
    # G16: one check per PR in Lint (always runs), no path-filtered workflow of its own.
    lint = dict(jobs(WORKFLOWS / "source.yml"))["lint-and-test"]
    assert "build-aux/ci/release_metadata.py --tag" in lint
    release = (WORKFLOWS / "release-check.yml").read_text()
    trigger = release.split("\njobs:", 1)[0]
    assert "pull_request" not in trigger and "workflow_dispatch:" in trigger


def test_package_builds_run_on_the_pinned_runner_image():
    # G16: the package builds run on the image every other job pins, not on
    # whatever ubuntu-latest points to. release.yml's publish job only uploads.
    for name in ("deb.yml", "packages.yml", "rpm-canary.yml"):
        images = re.findall(r"^    runs-on: (\S+)", (WORKFLOWS / name).read_text(), re.M)
        assert images and set(images) == {"ubuntu-26.04"}, (name, images)


def test_release_ships_the_flatpak_main_tested_and_refuses_it_without_an_update_remote():
    # G15: no rebuild at tag time, and no workflow calls Release preparation.
    release = (WORKFLOWS / "release.yml").read_text()
    flatpak = dict(jobs(WORKFLOWS / "release.yml"))["flatpak"]
    assert "release-check.yml" not in release and "name: flatpak-stable" in flatpak
    assert "run-id: ${{ needs.main-inputs.outputs.run_id }}" in flatpak
    assert "build-aux/ci/flatpak_bundle.py dist/cassini-talk.flatpak" in flatpak
    assert re.search(r"^    needs: \[deb, packages, flatpak\]$", release, re.M)
    assert not any("release-check.yml" in w.read_text() for w in WORKFLOWS.glob("*.yml"))
    stable = (WORKFLOWS / "flatpak.yml").read_text()
    assert "--repo-url" not in stable and "build-aux/ci/validate_installed.py" in stable
    assert "name: flatpak-stable" in stable
