# SPDX-License-Identifier: GPL-3.0-or-later
"""Release input provenance and CI accounting without publishing or GitHub writes."""

import importlib.util
from pathlib import Path

import pytest


def module(name):
    path = Path(__file__).parents[1] / "build-aux/ci" / (name + ".py")
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def runs():
    return [
        dict(
            id=i,
            path=".github/workflows/" + name,
            head_sha="a" * 40,
            event="push",
            head_branch="main",
            status="completed",
            conclusion="success",
            html_url="https://example.invalid/run/" + str(i),
        )
        for i, name in enumerate(module("green_main").WORKFLOWS, 1)
    ]


def test_release_requires_the_exact_commit_and_every_latest_main_workflow():
    policy = module("green_main")
    good = runs()
    assert set(policy.select_runs(good, "a" * 40)) == set(policy.WORKFLOWS)
    for altered in (
        dict(head_sha="b" * 40),
        dict(event="pull_request"),
        dict(head_branch="feature"),
        dict(status="in_progress", conclusion=None),
        dict(conclusion="failure"),
    ):
        bad = [dict(run, **altered) if run["id"] == 1 else run for run in good]
        with pytest.raises(ValueError):
            policy.select_runs(bad, "a" * 40)
    # A later failed run cannot borrow an earlier green run's artifacts.
    with pytest.raises(ValueError, match="not green"):
        policy.select_runs(good + [dict(good[0], id=100, conclusion="failure")], "a" * 40)


def test_release_requires_all_unexpired_package_artifacts():
    policy = module("green_main")
    good = [dict(name=name, expired=False) for name in policy.PACKAGES]
    policy.validate_artifacts(good)
    for changed in (good[:-1], good + [good[0]], [dict(a, expired=True) for a in good]):
        with pytest.raises(ValueError):
            policy.validate_artifacts(changed)


def test_ci_minutes_round_each_job_and_unstarted_jobs_are_not_falsely_complete():
    result = module("measure_ci").summarize(
        [
            dict(executionSeconds=61, queueSeconds=2),
            dict(executionSeconds=1, queueSeconds=8),
            dict(executionSeconds=None, queueSeconds=None),
        ]
    )
    assert result["jobCount"] == 3 and result["completedJobs"] == 2
    assert result["estimatedRoundedLinuxMinutes"] == 3
    assert result["executionMinutes"] == 62 / 60 and result["medianQueueSeconds"] == 5


def test_usage_api_minutes_are_separate_from_rounded_resource_estimates():
    usage = module("measure_ci").reported_minutes
    assert usage([{"UBUNTU": {"total_ms": 0}}]) == 0
    assert usage([{"UBUNTU": {"total_ms": 90000}}, {"UBUNTU": {"total_ms": 30000}}]) == 2
    assert usage([None]) is None and usage([]) is None
