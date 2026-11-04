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
