# SPDX-License-Identifier: GPL-3.0-or-later
"""G11: a package job proves from its Meson logs that it ran the whole pytest suite."""

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
SHARDS = 4
# A small checkout: which test files each shard must run.
EXPECTED = {1: {"tests/test_a.py"}, 2: {"tests/test_b.py", "tests/ux/test_c.py"}, 3: {"tests/test_d.py"}, 4: set()}
ALLOWED = "tests/test_ci_orchestrator.py::test_actual_git_rename_keeps_old_area_coverage"


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


def case(file, name, inner=""):
    module_name = file.removesuffix(".py").replace("/", ".")
    return f'<testcase classname="{module_name}" name="{name}" file="{file}">{inner}</testcase>'


def junit(cases):
    return f'<?xml version="1.0"?><testsuites><testsuite name="pytest">{"".join(cases)}</testsuite></testsuites>'


def passing(shard):
    return [case(file, f"test_{i}") for i, file in enumerate(sorted(EXPECTED[shard]))]


def logs(tmp_path, entries=None, cases=None, name="deb-debian-testing.1"):
    directory = tmp_path / "logs" / name
    directory.mkdir(parents=True)
    entries = [entry(i) for i in range(1, SHARDS + 1)] if entries is None else entries
    other = dict(name="cassini-talk:browser-protocol-conformance", result="OK", command=["x"], env={})
    (directory / "testlog.json").write_text("\n".join(json.dumps(e) for e in [other, *entries]) + "\n")
    for shard in range(1, SHARDS + 1):
        body = (cases or {}).get(shard, passing(shard))
        (directory / f"pytest-{shard}.junit.xml").write_text(junit(body))
    return directory


def check(directory):
    return module().problems(directory, SHARDS, EXPECTED)


def test_a_complete_passing_run_is_accepted(tmp_path):
    assert check(logs(tmp_path)) == []


def replace(shard, **changes):
    return [entry(i, **(changes if i == shard else {})) for i in range(1, SHARDS + 1)]


BAD_RUNS = {
    "a shard missing": [entry(i) for i in range(1, SHARDS)],
    "a shard twice": [*replace(0), entry(2)],
    "a failed shard": replace(2, result="FAIL"),
    "a skipped shard": replace(3, result="SKIP"),
    "a timed-out shard": replace(4, result="TIMEOUT"),
    "-k added": replace(1, command=entry(1)["command"][:-1] + ["-k", "nothing", "/build/src/tests"]),
    "one file instead of tests": replace(1, command=entry(1)["command"][:-1] + ["/build/src/tests/test_x.py"]),
    "--deselect added": replace(2, command=entry(2)["command"] + ["--deselect", "tests/test_x.py"]),
    "-p added": replace(2, command=entry(2)["command"][:1] + ["-p", "no:cacheprovider"] + entry(2)["command"][1:]),
    "wrong shard env": replace(1, env=dict(entry(1)["env"], CASSINI_TEST_SHARD="2/4")),
    "no GTK guard": replace(4, env={k: v for k, v in entry(4)["env"].items() if k != "CASSINI_TEST_REQUIRE_GTK"}),
    "PYTEST_ADDOPTS": replace(3, env=dict(entry(3)["env"], PYTEST_ADDOPTS="-k nothing")),
}


@pytest.mark.parametrize("name", BAD_RUNS)
def test_a_narrowed_or_incomplete_run_is_refused(tmp_path, name):
    assert check(logs(tmp_path, BAD_RUNS[name])), name


BAD_JUNIT = {
    # The red team's bypass: a file deleted before the test step (debian/rules
    # hook, %prep, prepare(), pytest config) leaves every shard passing.
    "a file deleted in the build": {2: [case("tests/test_b.py", "test_0")]},
    "a file run by another shard": {1: passing(1) + [case("tests/test_d.py", "test_9")], 3: []},
    "a GTK fallback skip": {
        1: passing(1)
        + [case("tests/test_a.py", "t", '<skipped message="Application bookkeeping also runs in GTK-enabled distro"/>')]
    },
    "a skip for a missing dependency": {3: [case("tests/test_d.py", "t", '<skipped message="no numpy"/>')]},
    "an xfail": {3: passing(3) + [case("tests/test_d.py", "t", '<skipped type="pytest.xfail" message=""/>')]},
    "a failure": {1: [case("tests/test_a.py", "t", '<failure message="boom"/>')]},
    "an error": {1: [case("tests/test_a.py", "t", '<error message="setup"/>')]},
}


@pytest.mark.parametrize("name", BAD_JUNIT)
def test_missing_files_unlisted_skips_or_failures_are_refused(tmp_path, name):
    assert check(logs(tmp_path, cases=BAD_JUNIT[name])), name


def test_only_the_listed_skips_are_allowed(tmp_path):
    skipped = {1: passing(1) + [case(*ALLOWED.split("::"), '<skipped message="no git"/>')]}
    expected = {**EXPECTED, 1: EXPECTED[1] | {"tests/test_ci_orchestrator.py"}}
    assert module().problems(logs(tmp_path, cases=skipped), SHARDS, expected) == []


def test_the_expected_files_are_this_checkouts_in_their_shards():
    check = module()
    expected = check.expected_files(SHARDS)
    files = sorted(f for shard in expected.values() for f in shard)
    assert "tests/test_check_pytest_ran.py" in files and "tests/conftest.py" not in files
    assert len(files) == len(set(files)) and all(f.startswith("tests/") for f in files)
    # Every listed skip names a test of this checkout.
    assert {node.partition("::")[0] for node in check.SKIPS} <= set(files)


def test_main_needs_exactly_one_log_directory_and_writes_its_slug(tmp_path, capsys, monkeypatch):
    check = module()
    assert check.main([str(tmp_path)]) == 1
    assert "0 Meson testlog.json" in capsys.readouterr().out
    logs(tmp_path, name="rpm-registry.opensuse.org-opensuse-tumbleweed-latest.1")
    monkeypatch.setattr(check, "expected_files", lambda shards: EXPECTED)
    output = tmp_path / "github-output"
    assert check.main([str(tmp_path / "logs"), "--github-output", str(output)]) == 0
    assert output.read_text() == "slug=registry.opensuse.org-opensuse-tumbleweed-latest\n"
    (tmp_path / "logs" / "deb-debian-testing.1").mkdir()
    (tmp_path / "logs" / "deb-debian-testing.1" / "testlog.json").write_text("")
    assert check.main([str(tmp_path / "logs")]) == 1


def test_main_refuses_a_missing_junit_file(tmp_path, capsys, monkeypatch):
    check = module()
    monkeypatch.setattr(check, "expected_files", lambda shards: EXPECTED)
    (logs(tmp_path) / "pytest-2.junit.xml").unlink()
    assert check.main([str(tmp_path / "logs")]) == 1
    assert "no pytest-2.junit.xml" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("names", "refused"),
    [
        ([], []),
        (["pytest-skipped-debian-testing", "pytest-ran-debian-testing", "meson-logs-deb-1"], []),
        (["pytest-ran-fedora-44"], []),  # A package job alone skips nothing.
        (["pytest-skipped-debian-testing", "pytest-ran-ubuntu-26.04"], ["debian-testing"]),
        (
            ["pytest-skipped-archlinux-latest", "pytest-skipped-debian-testing", "pytest-ran-archlinux-latest"],
            ["debian-testing"],
        ),
    ],
)
def test_the_gate_requires_a_proof_for_every_skip(tmp_path, names, refused):
    check = module()
    found = check.gate(names)
    assert [p.split(" on ")[1].split(" and ")[0] for p in found] == refused
    listing = tmp_path / "artifacts"
    listing.write_text("".join(n + "\n" for n in names))
    assert check.main(["--gate", str(listing)]) == (1 if refused else 0)
