# SPDX-License-Identifier: GPL-3.0-or-later
"""G11: package jobs prove which tests they ran; the gate compares them with Source."""

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
SHARDS = 4
# A small checkout: which test files each shard must run.
EXPECTED = {1: {"tests/test_a.py"}, 2: {"tests/test_b.py", "tests/ux/test_c.py"}, 3: {"tests/test_d.py"}, 4: set()}
DIGEST = "0" * 64
DEBIAN = 'PRETTY_NAME="Debian GNU/Linux forky/sid"\nID=debian\nVERSION_CODENAME=forky\n'
UBUNTU = 'ID=ubuntu\nVERSION_ID="26.04"\nVERSION_CODENAME=resolute\n'
FEDORA = "ID=fedora\nVERSION_ID=45\n"
FULL = {"tests.test_a::test_0": "passed", "tests.test_b::test_0": "passed"}


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


def run(**changes):
    return dict(dict(tests=DIGEST, inipath="/build/src/pyproject.toml", deselected=[]), **changes)


def logs(tmp_path, entries=None, cases=None, runs=None, name="deb-debian-testing.1", release=DEBIAN):
    directory = tmp_path / "logs" / name
    directory.mkdir(parents=True)
    entries = [entry(i) for i in range(1, SHARDS + 1)] if entries is None else entries
    other = dict(name="cassini-talk:browser-protocol-conformance", result="OK", command=["x"], env={})
    (directory / "testlog.json").write_text("\n".join(json.dumps(e) for e in [other, *entries]) + "\n")
    for shard in range(1, SHARDS + 1):
        (directory / f"pytest-{shard}.junit.xml").write_text(junit((cases or {}).get(shard, passing(shard))))
        record = (runs or {}).get(shard, run())
        if record is not None:
            (directory / f"pytest-{shard}.run.json").write_text(json.dumps(record))
    if release is not None:
        (directory / "os-release").write_text(release)
    return directory


def check(directory):
    return module().problems(directory, SHARDS, EXPECTED, DIGEST)


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
    # Round 1: a file deleted before the test step (debian/rules hook, %prep,
    # prepare(), pytest config) left every shard passing.
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


BAD_RECORDS = {
    # Round 2: a patched or rewritten tests/ tree, a pytest.ini written by the
    # recipe, a conftest hook that deselects only in package builds.
    "another tests tree": {2: run(tests="1" * 64)},
    "a pytest.ini": {1: run(inipath="/build/src/pytest.ini")},
    "no config file": {1: run(inipath="")},
    "a deselection": {3: run(deselected=["tests/test_d.py::test_recording"])},
    "no record": {4: None},
}


@pytest.mark.parametrize("name", BAD_RECORDS)
def test_a_changed_tree_config_or_selection_is_refused(tmp_path, name):
    assert check(logs(tmp_path, runs=BAD_RECORDS[name])), name


def test_logs_without_the_containers_os_release_are_refused(tmp_path):
    assert any("os-release" in p for p in check(logs(tmp_path, release=None)))


def test_only_the_listed_skips_are_allowed(tmp_path):
    classname, name = next(iter(module().SKIPS)).split("::")
    file = classname.replace(".", "/") + ".py"
    skipped = {1: passing(1) + [case(file, name, '<skipped message="no git"/>')]}
    expected = {**EXPECTED, 1: EXPECTED[1] | {file}}
    assert module().problems(logs(tmp_path, cases=skipped), SHARDS, expected, DIGEST) == []


@pytest.mark.parametrize("meson", ["OK", "FAIL", "SKIP", None])
def test_a_skip_meson_runs_instead_needs_that_meson_test_to_pass_in_the_same_log(tmp_path, meson):
    check = module()
    ((skip, test),) = check.MESON_RUNS.items()
    assert skip in check.SKIPS
    classname, name = skip.split("::")
    file = classname.replace(".", "/") + ".py"
    skipped = {1: passing(1) + [case(file, name, '<skipped message="Meson runs it"/>')]}
    directory = logs(tmp_path, cases=skipped)
    log = directory / "testlog.json"
    entries = [e for e in map(json.loads, log.read_text().splitlines()) if e["name"] != f"cassini-talk:{test}"]
    if meson is not None:
        entries.append(dict(name=f"cassini-talk:{test}", result=meson, command=["x"], env={}))
    log.write_text("\n".join(map(json.dumps, entries)) + "\n")
    found = check.problems(directory, SHARDS, {**EXPECTED, 1: EXPECTED[1] | {file}}, DIGEST)
    assert found == ([] if meson == "OK" else [f"{directory.name}: pytest-1 skipped {skip}, but {test} did not pass"])


def test_the_expected_files_and_skips_are_this_checkouts():
    check = module()
    expected = check.expected_files(SHARDS)
    files = sorted(f for shard in expected.values() for f in shard)
    assert "tests/test_check_pytest_ran.py" in files and "tests/conftest.py" not in files
    assert len(files) == len(set(files)) and all(f.startswith("tests/") for f in files)
    assert {n.partition("::")[0].replace(".", "/") + ".py" for n in check.SKIPS} <= set(files)


def test_the_digest_covers_every_copied_file_and_nothing_else(tmp_path):
    shard_plan = module().shard_plan
    (tmp_path / "data").mkdir()
    (tmp_path / "test_x.py").write_text("def test(): pass\n")
    (tmp_path / "data" / "vector.json").write_text("{}")
    before = shard_plan.tests_digest(tmp_path)
    for skipped in shard_plan.NOT_COPIED:
        (tmp_path / "data" / skipped).mkdir()
        (tmp_path / "data" / skipped / "x").write_bytes(b"x")
    assert shard_plan.tests_digest(tmp_path) == before
    (tmp_path / "data" / "vector.json").write_text('{"x": 1}')
    assert shard_plan.tests_digest(tmp_path) != before


def test_main_needs_one_log_directory_then_exports_its_nodes_and_slug(tmp_path, capsys, monkeypatch):
    check = module()
    assert check.main([str(tmp_path)]) == 1
    assert "0 Meson testlog.json" in capsys.readouterr().out
    logs(tmp_path, name="rpm-registry.opensuse.org-opensuse-tumbleweed-latest.1")
    monkeypatch.setattr(check, "expected_files", lambda shards: EXPECTED)
    monkeypatch.setattr(check.shard_plan, "tests_digest", lambda tests: DIGEST)
    output, out = tmp_path / "github-output", tmp_path / "export"
    assert check.main([str(tmp_path / "logs"), "--export", str(out), "--github-output", str(output)]) == 0
    assert output.read_text() == "slug=registry.opensuse.org-opensuse-tumbleweed-latest\n"
    assert set(json.loads((out / "nodes.json").read_text())) == {
        "tests.test_a::test_0",
        "tests.test_b::test_0",
        "tests.ux.test_c::test_1",
        "tests.test_d::test_0",
    }
    assert (out / "os-release").read_text() == DEBIAN
    (tmp_path / "logs" / "deb-debian-testing.1").mkdir()
    (tmp_path / "logs" / "deb-debian-testing.1" / "testlog.json").write_text("")
    assert check.main([str(tmp_path / "logs")]) == 1


@pytest.mark.parametrize(
    ("entries", "kind"),
    [
        (None, "nodes"),
        ([entry(i) for i in range(1, SHARDS)], "skipped"),  # --no-suite pytest, or a missing shard
        ([], "skipped"),
    ],
)
def test_a_source_job_records_its_nodes_or_a_skip_from_its_own_logs(tmp_path, entries, kind):
    check = module()
    logs(tmp_path, entries, name="distro-fedora-latest.1", release=FEDORA)
    out = tmp_path / "out"
    assert check.source(tmp_path / "logs", "fedora-latest", SHARDS, out) == (kind, "fedora-latest")
    assert (out / "nodes.json").is_file() == (kind == "nodes") and (out / "skipped").is_file() == (kind == "skipped")
    assert (out / "os-release").read_text() == FEDORA


def test_a_source_job_without_logs_for_its_image_records_a_skip(tmp_path, capsys):
    check = module()
    logs(tmp_path, name="distro-fedora-latest.1")
    assert check.source(tmp_path / "logs", "alpine-edge", SHARDS, tmp_path / "out")[0] == "skipped"
    output = tmp_path / "github-output"
    argv = ["--source", str(tmp_path / "missing"), "--slug", "alpine-edge", "--export", str(tmp_path / "out2")]
    assert check.main([*argv, "--github-output", str(output)]) == 0
    assert output.read_text() == "kind=skipped\nslug=alpine-edge\n"


def artifacts(tmp_path, kinds):
    """The gate's download: one directory per artifact name."""
    root = tmp_path / "pytest"
    root.mkdir()
    for name, (release, listed) in kinds.items():
        directory = root / name
        directory.mkdir()
        if release is not None:
            (directory / "os-release").write_text(release)
        if listed is not None:
            (directory / "nodes.json").write_text(json.dumps(listed))
        else:
            (directory / "skipped").write_text("skipped\n")
    return root


REFERENCE = {"pytest-nodes-fedora-latest": (FEDORA, FULL)}
GATES = {
    "nothing ran": ({}, []),
    "only Source": (REFERENCE, []),
    "a proven skip": (
        {**REFERENCE, "pytest-skipped-debian-testing": (DEBIAN, None), "pytest-ran-debian-testing": (DEBIAN, FULL)},
        [],
    ),
    # A skip with no proof: the package job was left out, or another image's job ran.
    "an unproven skip": ({**REFERENCE, "pytest-skipped-debian-testing": (DEBIAN, None)}, ["no package job"]),
    # Round 2: Source dropped pytest on its own (check-distros.sh, $GITHUB_ENV).
    "the reference skipped": ({"pytest-skipped-fedora-latest": (FEDORA, None)}, ["no package job"]),
    # Round 2: the deb leg ran its tests in ubuntu:26.04 under debian:testing's name.
    "another release": (
        {**REFERENCE, "pytest-skipped-debian-testing": (DEBIAN, None), "pytest-ran-debian-testing": (UBUNTU, FULL)},
        ["Source ran in"],
    ),
    # Round 2: a test left out only in package builds (deselected, defined
    # conditionally, patched away) is missing against the reference.
    "a missing test": (
        {
            **REFERENCE,
            "pytest-skipped-debian-testing": (DEBIAN, None),
            "pytest-ran-debian-testing": (DEBIAN, {"tests.test_a::test_0": "passed"}),
        },
        ["never ran 1 tests"],
    ),
    "one parameter left out": (
        {
            "pytest-nodes-fedora-latest": (
                FEDORA,
                dict(FULL, **{"tests.test_a::test_p[1]": "passed", "tests.test_a::test_p[2]": "passed"}),
            ),
            "pytest-ran-debian-testing": (DEBIAN, dict(FULL, **{"tests.test_a::test_p[1]": "passed"})),
        },
        ["never ran 1 tests"],
    ),
    # Another pytest release escapes a long parameter id differently: the same tests.
    "parameter ids escaped differently": (
        {
            "pytest-nodes-fedora-latest": (FEDORA, dict(FULL, **{"tests.test_a::test_p[\\xe9]": "passed"})),
            "pytest-ran-debian-testing": (DEBIAN, dict(FULL, **{"tests.test_a::test_p[\u00e9]": "passed"})),
        },
        [],
    ),
    "a skipped test": (
        {**REFERENCE, "pytest-ran-debian-testing": (DEBIAN, dict(FULL, **{"tests.test_b::test_0": "skipped"}))},
        ["did not pass"],
    ),
    "no reference": ({"pytest-ran-debian-testing": (DEBIAN, FULL)}, ["no pytest-nodes-fedora-latest"]),
}


@pytest.mark.parametrize("name", GATES)
def test_the_gate_requires_a_matching_complete_proof_for_every_skip(tmp_path, name):
    kinds, refused = GATES[name]
    check = module()
    found = check.gate(artifacts(tmp_path, kinds))
    assert len(found) == len(refused) and all(r in f for r, f in zip(refused, found, strict=True)), found
    assert check.main(["--gate", str(tmp_path / "pytest")]) == (1 if refused else 0)


def test_rolling_releases_compare_by_distribution_and_codename():
    check = module()
    tumbleweed = [check.os_release_text(f'ID=opensuse-tumbleweed\nVERSION_ID="2026110{d}"\n') for d in (3, 4)]
    assert check.release(tumbleweed[0]) == check.release(tumbleweed[1])
    older = check.os_release_text('ID=ubuntu\nVERSION_ID="25.10"\nVERSION_CODENAME=questing\n')
    assert check.release(check.os_release_text(UBUNTU)) != check.release(older)
