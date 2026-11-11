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


def test_a_source_job_that_failed_before_pytest_refuses_to_write_a_skip_marker(tmp_path, capsys):
    check = module()
    logs(tmp_path, name="distro-fedora-latest.1")
    with pytest.raises(ValueError, match="failed before meson test"):
        check.source(tmp_path / "logs", "alpine-edge", SHARDS, tmp_path / "out")
    assert not (tmp_path / "out").exists()
    output = tmp_path / "github-output"
    argv = ["--source", str(tmp_path / "missing"), "--slug", "alpine-edge", "--export", str(tmp_path / "out2")]
    assert check.main([*argv, "--github-output", str(output)]) == 1
    assert "failed before meson test" in capsys.readouterr().out
    assert not output.exists() and not (tmp_path / "out2").exists()
    logs(tmp_path / "failed_shard", replace(2, result="FAIL"), name="distro-alpine-edge.1", release="ID=alpine\n")
    with pytest.raises(ValueError, match="failed pytest shard"):
        check.source(tmp_path / "failed_shard/logs", "alpine-edge", SHARDS, tmp_path / "out3")
    assert not (tmp_path / "out3").exists()


def artifacts(tmp_path, kinds):
    """The gate's download: one directory per artifact name."""
    root = tmp_path / "pytest"
    root.mkdir(parents=True)
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
    found = check.compare_proofs(artifacts(tmp_path, kinds))
    assert len(found) == len(refused) and all(r in f for r, f in zip(refused, found, strict=True)), found
    # These relationship fixtures deliberately contain only part of the matrix.
    # The production CLI must reject even an internally consistent subset.
    assert check.main(["--gate", str(tmp_path / "pytest")]) == 1


def test_rolling_releases_compare_by_distribution_and_codename():
    check = module()
    tumbleweed = [check.os_release_text(f'ID=opensuse-tumbleweed\nVERSION_ID="2026110{d}"\n') for d in (3, 4)]
    assert check.release(tumbleweed[0]) == check.release(tumbleweed[1])
    older = check.os_release_text('ID=ubuntu\nVERSION_ID="25.10"\nVERSION_CODENAME=questing\n')
    assert check.release(check.os_release_text(UBUNTU)) != check.release(older)


def full_matrix():
    return {
        name: (DEBIAN, None if name.startswith("pytest-skipped-") else FULL) for name in module().REQUIRED_ARTIFACTS
    }


def test_complete_matrix_passes_the_production_gate(tmp_path):
    root = artifacts(tmp_path, full_matrix())
    check = module()
    assert check.gate(root) == []
    assert check.main(["--gate", str(root)]) == 0


@pytest.mark.parametrize("missing", module().REQUIRED_ARTIFACTS)
def test_no_matrix_job_can_disappear_even_if_its_dependents_are_missing(tmp_path, missing):
    kinds = full_matrix()
    del kinds[missing]
    root = artifacts(tmp_path, kinds)
    check = module()
    assert any(missing in problem for problem in check.gate(root))
    assert check.main(["--gate", str(root)]) == 1


def test_runtime_proof_manifest_matches_the_actual_workflow_matrices():
    import re

    def slug(image):
        return image.replace(":", "-").replace("/", "-")

    workflows = ROOT / ".github/workflows"
    source = (workflows / "source.yml").read_text()
    entries = re.findall(r'^          - image: (\S+)\n(?:            pytest-in: "([^"\n]+)"\n)?', source, re.M)
    expected = {"pytest-" + ("skipped-" if job else "nodes-") + slug(image) for image, job in entries}
    deb = json.loads(re.search(r"base: (\[[^\n]+\])", (workflows / "deb.yml").read_text())[1])
    rpm = re.findall(r"^          - base: (\S+)$", (workflows / "packages.yml").read_text(), re.M)
    arch = re.search(r"BASE=\$\{1:-(.+)\}", (ROOT / "build-aux/arch/build.sh").read_text())[1]
    expected.update("pytest-ran-" + slug(image) for image in [*deb, *rpm, arch])
    assert set(module().REQUIRED_ARTIFACTS) == expected


def test_missing_source_and_its_package_cannot_hide_each_other(tmp_path):
    kinds = full_matrix()
    del kinds["pytest-skipped-archlinux-latest"]
    del kinds["pytest-ran-archlinux-latest"]
    root = artifacts(tmp_path, kinds)
    check = module()
    assert check.compare_proofs(root) == []  # The old gate silently accepted it.
    assert len(check.gate(root)) == 2
    assert check.main(["--gate", str(root)]) == 1


ALPINE = "ID=alpine\nVERSION_ID=3.24.0_alpha20261101\n"
ALPINE_INFRA_MARKER = {
    "image": "alpine:edge",
    "step": "install-deps",
    "reason": "apk-upstream-mirror",
    "detail": "ERROR: onetbb-2023.2.0-r0: v2 package integrity error",
}


def write_infra_skip(directory, marker=ALPINE_INFRA_MARKER, release=ALPINE):
    directory.mkdir(parents=True, exist_ok=True)
    if marker is not None:
        (directory / "infra-skipped.json").write_text(json.dumps(marker) + "\n")
    if release is not None:
        (directory / "os-release").write_text(release)
    return directory


def test_rerun_attempt_artifacts_supersede_stale_skip_markers(tmp_path):
    check = module()
    # Partial re-run ("Re-run failed jobs"): other jobs keep attempt 1, while
    # alpine-edge left a stale skip marker on attempt 1 and passed on attempt 2.
    attempt1 = {f"{name}-attempt-1": value for name, value in full_matrix().items() if not name.endswith("alpine-edge")}
    attempt1["pytest-skipped-alpine-edge-attempt-1"] = (ALPINE, None)
    attempt1["pytest-nodes-alpine-edge-attempt-2"] = (ALPINE, FULL)
    root = artifacts(tmp_path / "rerun_pass", attempt1)
    assert check.gate(root) == []
    assert check.main(["--gate", str(root)]) == 0

    # Stale infra-skip on attempt 1 superseded by passing nodes on attempt 2.
    root_infra = artifacts(
        tmp_path / "rerun_from_infra",
        {
            **{k: v for k, v in attempt1.items() if not k.startswith("pytest-skipped-alpine-edge")},
        },
    )
    write_infra_skip(root_infra / "pytest-infra-skipped-alpine-edge-attempt-1")
    assert check.gate(root_infra) == []

    # Reverse direction: passing attempt 1 must NOT mask a skip on attempt 2.
    regressed = {
        f"{name}-attempt-1": value for name, value in full_matrix().items() if not name.endswith("alpine-edge")
    }
    regressed["pytest-nodes-alpine-edge-attempt-1"] = (ALPINE, FULL)
    regressed["pytest-skipped-alpine-edge-attempt-2"] = (ALPINE, None)
    root_regressed = artifacts(tmp_path / "rerun_regressed", regressed)
    assert any("pytest-nodes-alpine-edge/nodes.json" in p for p in check.gate(root_regressed))
    assert check.main(["--gate", str(root_regressed)]) == 1

    # Same-attempt conflicting markers must fail explicitly.
    same_attempt = dict(full_matrix())
    same_attempt["pytest-skipped-alpine-edge"] = (ALPINE, None)
    root_conflict = artifacts(tmp_path / "same_attempt", same_attempt)
    assert any("conflicting source artifacts for alpine-edge" in p for p in check.gate(root_conflict))


def test_alpine_edge_install_infra_skip_accepted_only_for_alpine_install_failures(tmp_path, capsys):
    check = module()
    # Source export + full gate acceptance for genuine alpine:edge install failure.
    write_infra_skip(tmp_path / "meson-logs/distro-alpine-edge.1")
    out, gh_out = tmp_path / "exported", tmp_path / "gh-output"
    assert (
        check.main(
            [
                "--source",
                str(tmp_path / "meson-logs"),
                "--slug",
                "alpine-edge",
                "--export",
                str(out),
                "--github-output",
                str(gh_out),
            ]
        )
        == 0
    )
    assert gh_out.read_text() == "kind=infra-skipped\nslug=alpine-edge\n"
    assert "::warning::alpine-edge: upstream install infrastructure skip" in capsys.readouterr().out

    kinds = {f"{k}-attempt-1": v for k, v in full_matrix().items() if k != "pytest-nodes-alpine-edge"}
    root = artifacts(tmp_path / "gate_ok", kinds)
    write_infra_skip(root / "pytest-infra-skipped-alpine-edge-attempt-1")
    assert check.gate(root) == []
    assert check.main(["--gate", str(root)]) == 0
    assert "::warning::Pytest coverage accepted alpine:edge install infrastructure skip" in capsys.readouterr().out

    # Rejected on every other distro even with identical marker contents.
    for other in ("fedora-latest", "debian-testing", "ubuntu-26.04", "archlinux-latest"):
        bad_root = artifacts(
            tmp_path / f"other_{other}",
            {k: v for k, v in full_matrix().items() if not k.endswith(other) or k.startswith("pytest-ran-")},
        )
        write_infra_skip(bad_root / f"pytest-infra-skipped-{other}")
        assert any("only allowed for alpine-edge" in p for p in check.gate(bad_root)), other

    # Rejected on alpine-edge for build, test, smoke, wrong reason, empty detail, or wrong os-release.
    invalid_cases = [
        (dict(ALPINE_INFRA_MARKER, step="meson setup"), ALPINE, "install-deps"),
        (dict(ALPINE_INFRA_MARKER, step="meson test"), ALPINE, "install-deps"),
        (dict(ALPINE_INFRA_MARKER, step="runtime smoke test"), ALPINE, "install-deps"),
        (dict(ALPINE_INFRA_MARKER, reason="test-flake"), ALPINE, "apk-upstream-mirror"),
        (dict(ALPINE_INFRA_MARKER, detail="   "), ALPINE, "detail"),
        (dict(ALPINE_INFRA_MARKER, image="fedora:latest"), ALPINE, "alpine:edge"),
        (ALPINE_INFRA_MARKER, DEBIAN, "os-release"),
        (None, ALPINE, "missing infra-skipped.json"),
    ]
    for idx, (marker, rel, expected_msg) in enumerate(invalid_cases):
        bad_root = artifacts(
            tmp_path / f"bad_alpine_{idx}",
            {k: v for k, v in full_matrix().items() if k != "pytest-nodes-alpine-edge"},
        )
        write_infra_skip(bad_root / "pytest-infra-skipped-alpine-edge", marker=marker, release=rel)
        found = check.gate(bad_root)
        assert found and any(expected_msg in p for p in found), (idx, found)


def test_alpine_edge_install_script_switches_mirror_and_marks_only_upstream_install_failures(tmp_path):
    import os
    import subprocess

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    sleep_bin = bin_dir / "sleep"
    sleep_bin.write_text("#!/bin/sh\nexit 0\n")
    sleep_bin.chmod(0o755)

    def run_case(name, behavior):
        case_dir = tmp_path / name
        logs_dir = case_dir / "logs"
        cache_dir = case_dir / "cache"
        logs_dir.mkdir(parents=True)
        cache_dir.mkdir(parents=True)
        (cache_dir / "stale.apk").write_text("corrupt")
        repos = case_dir / "repositories"
        repos.write_text(
            "https://dl-cdn.alpinelinux.org/alpine/edge/main\nhttps://dl-cdn.alpinelinux.org/alpine/edge/community\n"
        )
        installer = case_dir / "install-deps.sh"
        installer.write_text(f"#!/bin/sh\nset -eu\n{behavior}\n")
        installer.chmod(0o755)
        env = dict(
            os.environ,
            PATH=f"{bin_dir}:{os.environ['PATH']}",
            CASSINI_INSTALL_DEPS=str(installer),
            ALPINE_REPOS_FILE=str(repos),
            ALPINE_APK_CACHE=str(cache_dir),
            ALPINE_INFRA_SKIP_DIR=str(logs_dir),
            CASSINI_ALPINE_FALLBACK_MIRROR="https://mirrors.edge.kernel.org/alpine",
        )
        proc = subprocess.run(
            ["sh", str(ROOT / "build-aux/ci/alpine_edge_install.sh")],
            env=env,
            text=True,
            capture_output=True,
        )
        return proc, repos, logs_dir, cache_dir

    # 1. Primary mirror fails integrity -> fallback mirror succeeds -> exit 0, no infra-skip.
    proc, repos, logs_dir, cache_dir = run_case(
        "fallback_ok",
        f"""
if grep -q 'dl-cdn.alpinelinux.org' "{tmp_path}/fallback_ok/repositories"; then
  echo "ERROR: onetbb-2023.2.0-r0: v2 package integrity error" >&2
  exit 1
fi
exit 0
""",
    )
    assert proc.returncode == 0
    assert "https://mirrors.edge.kernel.org/alpine/edge/main" in repos.read_text()
    assert not (cache_dir / "stale.apk").exists()
    assert not (logs_dir / "infra-skipped.json").exists()
    assert "switching to fallback mirror" in proc.stderr

    # 2. Both mirrors fail with upstream apk integrity/fetch errors -> exit 75 + infra-skipped.json.
    proc, repos, logs_dir, _ = run_case(
        "both_fail_upstream",
        'echo "ERROR: onetbb-2023.2.0-r0: v2 package integrity error" >&2; exit 1',
    )
    assert proc.returncode == 75
    assert "::warning::alpine:edge canary infrastructure skip" in proc.stderr
    marker = json.loads((logs_dir / "infra-skipped.json").read_text())
    assert marker == {
        "image": "alpine:edge",
        "step": "install-deps",
        "reason": "apk-upstream-mirror",
        "detail": "ERROR: onetbb-2023.2.0-r0: v2 package integrity error",
    }

    # 3. Non-upstream apk error (our own broken package name) -> exit 1 immediately, no fallback or skip!
    proc, repos, logs_dir, _ = run_case(
        "our_packaging_bug",
        'echo "ERROR: unable to select packages: cassini-typo-pkg (no such package)" >&2; exit 2',
    )
    assert proc.returncode == 2
    assert "dl-cdn.alpinelinux.org" in repos.read_text()
    assert not (logs_dir / "infra-skipped.json").exists()
