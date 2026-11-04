# SPDX-License-Identifier: GPL-3.0-or-later
"""G11: a package job proves from its Meson logs that it ran the whole pytest suite."""

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
SHARDS = 4


def module():
    spec = importlib.util.spec_from_file_location("check_pytest_ran", ROOT / "build-aux/ci/check_pytest_ran.py")
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def entry(shard, build="/build/src/obj-x86_64-linux-gnu", **changes):
    value = dict(
        name=f"cassini-talk:pytest-{shard}",
        result="OK",
        command=[
            "/usr/bin/python3",
            *module().ARGUMENTS,
            f"--junitxml={build}/meson-logs/pytest-{shard}.junit.xml",
            "/build/src/tests",
        ],
        env=dict(CASSINI_TEST_SHARD=f"{shard}/{SHARDS}", CASSINI_TEST_REQUIRE_GTK="1", CASSINI_TEST_TIMINGS="1"),
    )
    value.update(changes)
    return value


def junit(cases):
    body = "".join(
        f'<testcase classname="tests.test_x" name="t{i}">{inner}</testcase>' for i, inner in enumerate(cases)
    )
    return f'<?xml version="1.0"?><testsuites><testsuite name="pytest">{body}</testsuite></testsuites>'


def logs(tmp_path, entries=None, cases=None):
    directory = tmp_path / "logs" / "deb-debian-testing.1"
    directory.mkdir(parents=True)
    entries = [entry(i) for i in range(1, SHARDS + 1)] if entries is None else entries
    other = dict(name="cassini-talk:browser-protocol-conformance", result="OK", command=["x"], env={})
    (directory / "testlog.json").write_text("\n".join(json.dumps(e) for e in [other, *entries]) + "\n")
    for shard in range(1, SHARDS + 1):
        (directory / f"pytest-{shard}.junit.xml").write_text(junit(cases or ["", '<skipped message="no display"/>']))
    return directory


def test_a_complete_passing_run_is_accepted(tmp_path):
    assert module().problems(logs(tmp_path), SHARDS) == []
    assert module().main([str(tmp_path / "logs")]) == 0


def replace(shard, **changes):
    return [entry(i, **(changes if i == shard else {})) for i in range(1, SHARDS + 1)]


BAD = {
    "a shard missing": [entry(i) for i in range(1, SHARDS)],
    "a shard twice": [*replace(0), entry(2)],
    "a failed shard": replace(2, result="FAIL"),
    "a skipped shard": replace(3, result="SKIP"),
    "a timed-out shard": replace(4, result="TIMEOUT"),
    "-k added": replace(1, command=entry(1)["command"][:-1] + ["-k", "nothing", "/build/src/tests"]),
    "one file instead of tests": replace(1, command=entry(1)["command"][:-1] + ["/build/src/tests/test_x.py"]),
    "--deselect added": replace(2, command=entry(2)["command"] + ["--deselect", "tests/test_x.py"]),
    "-p no:something": replace(
        2, command=entry(2)["command"][:1] + ["-p", "no:cacheprovider"] + entry(2)["command"][1:]
    ),
    "wrong shard env": replace(1, env=dict(entry(1)["env"], CASSINI_TEST_SHARD="2/4")),
    "no GTK guard": replace(4, env={k: v for k, v in entry(4)["env"].items() if k != "CASSINI_TEST_REQUIRE_GTK"}),
    "PYTEST_ADDOPTS": replace(3, env=dict(entry(3)["env"], PYTEST_ADDOPTS="-k nothing")),
}


@pytest.mark.parametrize("name", BAD)
def test_a_narrowed_or_incomplete_run_is_refused(tmp_path, name):
    assert module().problems(logs(tmp_path, BAD[name]), SHARDS), name


@pytest.mark.parametrize(
    "cases",
    [
        ['<skipped message="Application bookkeeping also runs in GTK-enabled distro and Recording jobs"/>', ""],
        ['<skipped message="Meeting details dates also run in GTK-enabled distro and Recording jobs"/>', ""],
        ['<failure message="boom"/>', ""],
        ['<error message="setup"/>', ""],
        ['<skipped message="no display"/>'],
    ],
)
def test_junit_with_gtk_fallback_skips_failures_or_no_passing_test_is_refused(tmp_path, cases):
    assert module().problems(logs(tmp_path, cases=cases), SHARDS)


def test_missing_logs_or_junit_are_refused(tmp_path, capsys):
    assert module().main([str(tmp_path)]) == 1
    assert "no Meson testlog.json" in capsys.readouterr().out
    directory = logs(tmp_path)
    (directory / "pytest-2.junit.xml").unlink()
    assert module().main([str(tmp_path / "logs")]) == 1
    assert "no pytest-2.junit.xml" in capsys.readouterr().out
