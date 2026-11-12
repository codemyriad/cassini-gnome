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


def pr_matrix():
    return {
        name: (UBUNTU if "ubuntu" in name else FEDORA, None if name.startswith("pytest-skipped-") else FULL)
        for name in module().REQUIRED_PR_ARTIFACTS
    }


def test_complete_pr_tier_matrix_passes_the_pr_gate_and_rejects_missing_or_infra_skip(tmp_path):
    check = module()
    root = artifacts(tmp_path / "ok", pr_matrix())
    assert check.gate(root, tier="pr") == []
    assert check.main(["--gate", str(root), "--tier", "pr"]) == 0
    # Default --gate still requires the full 11-artifact matrix.
    assert check.main(["--gate", str(root)]) == 1
    for missing in check.REQUIRED_PR_ARTIFACTS:
        kinds = pr_matrix()
        del kinds[missing]
        bad = artifacts(tmp_path / f"missing-{missing}", kinds)
        assert any(missing in problem for problem in check.gate(bad, tier="pr"))
        assert check.main(["--gate", str(bad), "--tier", "pr"]) == 1
    # Alpine infra-skip is never accepted in the pr tier.
    with_infra = artifacts(tmp_path / "infra", pr_matrix())
    write_infra_skip(with_infra / "pytest-infra-skipped-alpine-edge")
    assert any("pr tier" in problem for problem in check.gate(with_infra, tier="pr"))


def test_runtime_proof_manifest_matches_the_actual_workflow_matrices(tmp_path):
    import itertools
    import re

    def slug(image):
        return image.split("@", 1)[0].replace(":", "-").replace("/", "-")

    def two_branches(text, key, input_name):
        match = re.search(
            rf"{key}: \$\{{\{{ inputs\.{input_name} && fromJSON\('([^']+)'\) \|\| fromJSON\('([^']+)'\) \}}\}}",
            text,
        )
        assert match is not None, (key, input_name)
        return json.loads(match[1]), json.loads(match[2])

    workflows = ROOT / ".github/workflows"
    source_text = (workflows / "source.yml").read_text()
    source_match = re.search(
        r"include: \$\{\{ fromJSON\(format\('([^']+)', "
        r"inputs\.extra_distros && '([^']*)' \|\| '([^']*)', "
        r"inputs\.deb_testing && '([^']*)' \|\| '([^']*)', "
        r"inputs\.rpm_opensuse && '([^']*)' \|\| '([^']*)', "
        r"inputs\.arch && '([^']*)' \|\| '([^']*)'\)\) \}\}",
        source_text,
    )
    assert source_match is not None
    fmt, extra_t, extra_f, deb_t, deb_f, rpm_t, rpm_f, arch_t, arch_f = source_match.groups()

    deb_full, deb_pr = two_branches((workflows / "deb.yml").read_text(), "base", "deb_testing")
    rpm_full, rpm_pr = two_branches((workflows / "packages.yml").read_text(), "include", "rpm_opensuse")
    arch_image = re.search(r"BASE=\$\{1:-(.+)\}", (ROOT / "build-aux/arch/build.sh").read_text())[1]
    assert re.search(
        r"  arch:\n    name: Build Arch package\n    if: \$\{\{ inputs\.arch \}\}\n",
        (workflows / "packages.yml").read_text(),
    )

    check = module()
    for idx, (deb_testing, rpm_opensuse, arch, extra_distros) in enumerate(itertools.product((False, True), repeat=4)):
        source_entries = json.loads(
            fmt.format(
                extra_t if extra_distros else extra_f,
                deb_t if deb_testing else deb_f,
                rpm_t if rpm_opensuse else rpm_f,
                arch_t if arch else arch_f,
            )
        )
        deb_entries = deb_full if deb_testing else deb_pr
        rpm_entries = rpm_full if rpm_opensuse else rpm_pr
        arch_entries = [arch_image] if arch else []

        expected = {
            "pytest-" + ("skipped-" if item.get("pytest-in") else "nodes-") + slug(item["image"])
            for item in source_entries
        }
        expected.update(
            "pytest-ran-" + slug(image) for image in [*deb_entries, *(r["base"] for r in rpm_entries), *arch_entries]
        )
        required, reference_slug = check.expected_gate(
            deb_testing=deb_testing,
            rpm_opensuse=rpm_opensuse,
            arch=arch,
            extra_distros=extra_distros,
        )
        assert set(required) == expected
        assert reference_slug == ("fedora-latest" if extra_distros else "fedora-44")

        combo_root = artifacts(
            tmp_path / f"combo-{idx}",
            {name: (DEBIAN, None if name.startswith("pytest-skipped-") else FULL) for name in required},
        )
        assert (
            check.gate(
                combo_root,
                deb_testing=deb_testing,
                rpm_opensuse=rpm_opensuse,
                arch=arch,
                extra_distros=extra_distros,
            )
            == []
        )
        assert (
            check.main(
                [
                    "--gate",
                    str(combo_root),
                    "--changes-result",
                    "success",
                    "--deb-testing",
                    str(deb_testing).lower(),
                    "--rpm-opensuse",
                    str(rpm_opensuse).lower(),
                    "--arch",
                    str(arch).lower(),
                    "--extra-distros",
                    str(extra_distros).lower(),
                ]
            )
            == 0
        )

    assert set(check.expected_gate()[0]) == set(check.REQUIRED_ARTIFACTS)
    assert (
        check.expected_gate(deb_testing=False, rpm_opensuse=False, arch=False, extra_distros=False)[0]
        == check.REQUIRED_PR_ARTIFACTS
    )

    # Failed or missing changes outputs fail open to requiring the full matrix.
    pr_only_root = artifacts(tmp_path / "pr-only", pr_matrix())
    assert (
        check.main(
            [
                "--gate",
                str(pr_only_root),
                "--changes-result",
                "failure",
                "--deb-testing",
                "false",
                "--rpm-opensuse",
                "false",
                "--arch",
                "false",
                "--extra-distros",
                "false",
            ]
        )
        == 1
    )
    assert (
        check.main(
            [
                "--gate",
                str(pr_only_root),
                "--changes-result",
                "success",
                "--deb-testing",
                "",
                "--rpm-opensuse",
                "false",
                "--arch",
                "false",
                "--extra-distros",
                "false",
            ]
        )
        == 1
    )


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


def write_installed_outcome(directory, status="installed", release=ALPINE):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "outcome.json").write_text(
        json.dumps({"image": "alpine:edge", "step": "install-deps", "status": status}) + "\n"
    )
    if release is not None:
        (directory / "os-release").write_text(release)
    return directory


def test_alpine_edge_install_infra_skip_accepted_only_for_alpine_install_failures(tmp_path, capsys):
    check = module()
    # Source export + full gate acceptance for genuine Stage 1 alpine:edge install failure.
    outcome_root = tmp_path / "install-outcome"
    meson_logs = tmp_path / "meson-logs"
    meson_logs.mkdir()
    write_infra_skip(outcome_root / "distro-alpine-edge")
    out, gh_out = tmp_path / "exported", tmp_path / "gh-output"
    assert (
        check.main(
            [
                "--source",
                str(meson_logs),
                "--install-outcome",
                str(outcome_root),
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

    # Without --install-outcome, source() never exports infra-skipped.
    with pytest.raises(ValueError, match="failed before meson test"):
        check.source(meson_logs, "alpine-edge", SHARDS, tmp_path / "no_outcome_flag")

    # Overlapping install-outcome and logs paths are rejected.
    with pytest.raises(ValueError, match="overlaps test-writable logs"):
        check.source(meson_logs, "alpine-edge", SHARDS, tmp_path / "overlap1", install_outcome=meson_logs)
    with pytest.raises(ValueError, match="overlaps test-writable logs"):
        check.source(meson_logs, "alpine-edge", SHARDS, tmp_path / "overlap2", install_outcome=meson_logs / "sub")

    # Accompanying build/test logs for alpine-edge (even without testlog.json) invalidate an install infra-skip.
    leftover = meson_logs / "distro-alpine-edge.1"
    leftover.mkdir()
    (leftover / "testlog.txt").write_text("ran tests\n")
    with pytest.raises(ValueError, match="cannot accompany test logs"):
        check.source(meson_logs, "alpine-edge", SHARDS, tmp_path / "with_logs", install_outcome=outcome_root)

    # Normal alpine-edge run with --install-outcome requires status == "installed".
    normal_logs = tmp_path / "normal"
    logs(normal_logs, name="distro-alpine-edge.1", release=ALPINE)
    normal_outcome = tmp_path / "normal-outcome"
    write_installed_outcome(normal_outcome / "distro-alpine-edge", status="installed")
    assert check.source(
        normal_logs / "logs", "alpine-edge", SHARDS, tmp_path / "normal_out", install_outcome=normal_outcome
    ) == ("nodes", "alpine-edge")
    write_installed_outcome(normal_outcome / "distro-alpine-edge", status="failed")
    with pytest.raises(ValueError, match="install stage did not complete normally"):
        check.source(
            normal_logs / "logs", "alpine-edge", SHARDS, tmp_path / "failed_out", install_outcome=normal_outcome
        )

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

    # Rejected on alpine-edge for build, test, smoke, non-ERROR detail, wrong reason, empty detail, or wrong os-release.
    invalid_cases = [
        (dict(ALPINE_INFRA_MARKER, step="meson setup"), ALPINE, "install-deps"),
        (dict(ALPINE_INFRA_MARKER, step="meson test"), ALPINE, "install-deps"),
        (dict(ALPINE_INFRA_MARKER, step="runtime smoke test"), ALPINE, "install-deps"),
        (dict(ALPINE_INFRA_MARKER, reason="test-flake"), ALPINE, "apk-upstream-mirror"),
        (dict(ALPINE_INFRA_MARKER, detail="   "), ALPINE, "detail"),
        (dict(ALPINE_INFRA_MARKER, detail="Installing gnutls (3.8.13-r0)"), ALPINE, "ERROR detail"),
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

    # Extra files inside the infra-skipped artifact directory are rejected.
    extra_root = artifacts(
        tmp_path / "extra_file_alpine",
        {k: v for k, v in full_matrix().items() if k != "pytest-nodes-alpine-edge"},
    )
    extra_dir = write_infra_skip(extra_root / "pytest-infra-skipped-alpine-edge")
    (extra_dir / "testlog.txt").write_text("unexpected\n")
    assert any("unexpected files" in p for p in check.gate(extra_root))


def test_test_process_cannot_forge_alpine_infra_skip_in_writable_logs(tmp_path):
    import os
    import shutil
    import subprocess
    import sys

    check = module()
    outcome_root = tmp_path / "install-outcome"
    outcome_dir = write_installed_outcome(outcome_root / "distro-alpine-edge", status="installed")
    for item in outcome_dir.iterdir():
        item.chmod(0o444)
    outcome_dir.chmod(0o555)

    forger_script = tmp_path / "forger.py"
    forger_script.write_text(
        "import json, os, pathlib\n"
        "for target in [os.environ['FORGE_BUILD_LOGS'], os.environ['FORGE_SAVED_LOGS']]:\n"
        "    if not target:\n"
        "        continue\n"
        "    p = pathlib.Path(target)\n"
        "    p.mkdir(parents=True, exist_ok=True)\n"
        "    tlog = p / 'testlog.json'\n"
        "    if tlog.exists():\n"
        "        tlog.unlink()\n"
        f"    (p / 'infra-skipped.json').write_text({json.dumps(json.dumps(ALPINE_INFRA_MARKER))})\n"
        f"    (p / 'os-release').write_text({json.dumps(ALPINE)})\n"
    )

    try:
        meson_bin = shutil.which("meson")
        if os.environ.get("CASSINI_TEST_SHARD"):
            assert meson_bin is not None, "meson must be installed in CI shard runs"
        if meson_bin is not None:
            proj = tmp_path / "meson_forger"
            build_dir = proj / "b"
            saved_logs_root = tmp_path / "meson_saved_logs"
            saved_distro = saved_logs_root / "distro-alpine-edge.1"
            proj.mkdir()
            (proj / "meson.build").write_text(
                "project('forge-repro', version: '0.1')\n"
                f"py = find_program({sys.executable!r})\n"
                f"test('forger', py, args: [{str(forger_script)!r}],\n"
                f"  env: ['FORGE_BUILD_LOGS=' + meson.current_build_dir() / 'meson-logs', 'FORGE_SAVED_LOGS={saved_distro}'])\n"
            )
            subprocess.run([meson_bin, "setup", str(build_dir), str(proj)], check=True, capture_output=True, text=True)
            test_proc = subprocess.run(
                [meson_bin, "test", "-C", str(build_dir), "--print-errorlogs"],
                check=False,
                capture_output=True,
                text=True,
            )
            assert test_proc.returncode == 0, test_proc.stdout + test_proc.stderr
            shutil.copytree(build_dir / "meson-logs", saved_distro, dirs_exist_ok=True)
            assert not (saved_distro / "testlog.json").exists()
            assert (saved_distro / "testlog.txt").is_file()
            assert (saved_distro / "infra-skipped.json").is_file()
            out_meson = tmp_path / "meson_out"
            assert (
                check.main(
                    [
                        "--source",
                        str(saved_logs_root),
                        "--install-outcome",
                        str(outcome_root),
                        "--slug",
                        "alpine-edge",
                        "--export",
                        str(out_meson),
                    ]
                )
                == 1
            )
            assert not out_meson.exists()
            assert (
                check.main(["--source", str(saved_logs_root), "--slug", "alpine-edge", "--export", str(out_meson)]) == 1
            )

        # Direct child test process holding an open fd to testlog.json while unlinking it and writing forged marker.
        proc_logs_root = tmp_path / "proc_logs"
        proc_distro = proc_logs_root / "distro-alpine-edge.1"
        proc_distro.mkdir(parents=True)
        (proc_distro / "testlog.txt").write_text("1/1 forger OK\n")
        (proc_distro / "pytest-1.junit.xml").write_text(junit(passing(1)))
        with (proc_distro / "testlog.json").open("w") as open_testlog:
            open_testlog.write(json.dumps({"name": "forger", "result": "OK"}) + "\n")
            open_testlog.flush()
            child = subprocess.run(
                [sys.executable, str(forger_script)],
                env=dict(os.environ, FORGE_BUILD_LOGS=str(proc_distro), FORGE_SAVED_LOGS=""),
                check=False,
                capture_output=True,
                text=True,
            )
            assert child.returncode == 0
            open_testlog.write(json.dumps({"name": "pytest-1", "result": "OK"}) + "\n")
            open_testlog.flush()
        assert not (proc_distro / "testlog.json").exists()
        assert (proc_distro / "infra-skipped.json").is_file()
        with pytest.raises(ValueError, match="untrusted infra-skipped.json"):
            check.source(proc_logs_root, "alpine-edge", SHARDS, tmp_path / "forged_out", install_outcome=outcome_root)
        with pytest.raises(ValueError, match="untrusted infra-skipped.json"):
            check.source(proc_logs_root, "alpine-edge", SHARDS, tmp_path / "forged_out_no_flag")

        # Child wipes testlog.json without planting infra-skipped.json: trusted installed outcome still requires testlog.json.
        (proc_distro / "infra-skipped.json").unlink()
        with pytest.raises(ValueError, match="0 Meson testlog.json directories"):
            check.source(proc_logs_root, "alpine-edge", SHARDS, tmp_path / "wiped_out", install_outcome=outcome_root)
    finally:
        outcome_dir.chmod(0o755)
        for item in outcome_dir.iterdir():
            item.chmod(0o644)


def test_alpine_edge_install_script_switches_mirror_and_marks_only_upstream_install_failures(tmp_path):
    import os
    import subprocess

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    sleep_bin = bin_dir / "sleep"
    sleep_bin.write_text("#!/bin/sh\nexit 0\n")
    sleep_bin.chmod(0o755)

    apk_progress = """cat <<'PROGRESS'
fetch https://dl-cdn.alpinelinux.org/alpine/edge/main/x86_64/APKINDEX.tar.gz
fetch https://dl-cdn.alpinelinux.org/alpine/edge/community/x86_64/APKINDEX.tar.gz
(1/324) Installing musl (1.2.5-r10)
(195/324) Installing gnutls (3.8.13-r0)
(196/324) Installing openssl (3.5.0-r0)
PROGRESS"""

    def run_case(name, behavior):
        case_dir = tmp_path / name
        outcome_dir = case_dir / "install-outcome"
        cache_dir = case_dir / "cache"
        outcome_dir.mkdir(parents=True)
        cache_dir.mkdir(parents=True)
        (cache_dir / "stale.apk").write_text("corrupt")
        os_rel = case_dir / "os-release"
        os_rel.write_text(ALPINE)
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
            ALPINE_INSTALL_OUTCOME_DIR=str(outcome_dir),
            ALPINE_OS_RELEASE=str(os_rel),
            CASSINI_ALPINE_FALLBACK_MIRROR="https://mirrors.edge.kernel.org/alpine",
        )
        proc = subprocess.run(
            ["sh", str(ROOT / "build-aux/ci/alpine_edge_install.sh")],
            env=env,
            text=True,
            capture_output=True,
        )
        return proc, repos, outcome_dir, cache_dir

    # 1. Primary mirror fails integrity -> fallback mirror succeeds -> exit 0, outcome=installed, no infra-skip.
    proc, repos, outcome_dir, cache_dir = run_case(
        "fallback_ok",
        f"""
{apk_progress}
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
    assert not (outcome_dir / "infra-skipped.json").exists()
    assert json.loads((outcome_dir / "outcome.json").read_text())["status"] == "installed"
    assert "switching to fallback mirror" in proc.stderr

    # 2. Both mirrors fail with explicit upstream apk integrity/fetch ERROR diagnostics -> exit 75 + infra-skipped.json.
    proc, repos, outcome_dir, _ = run_case(
        "both_fail_upstream",
        f"""
{apk_progress}
if grep -q 'dl-cdn.alpinelinux.org' "{tmp_path}/both_fail_upstream/repositories"; then
  echo "ERROR: onetbb-2023.2.0-r0: v2 package integrity error" >&2
else
  echo "ERROR: https://mirrors.edge.kernel.org/alpine/edge/main: TLS handshake failed" >&2
fi
exit 1
""",
    )
    assert proc.returncode == 75
    assert "::warning::alpine:edge canary infrastructure skip" in proc.stderr
    assert not (outcome_dir / "outcome.json").exists()
    assert (outcome_dir / "os-release").read_text() == ALPINE
    marker = json.loads((outcome_dir / "infra-skipped.json").read_text())
    assert marker == {
        "image": "alpine:edge",
        "step": "install-deps",
        "reason": "apk-upstream-mirror",
        "detail": "ERROR: https://mirrors.edge.kernel.org/alpine/edge/main: TLS handshake failed",
    }

    # 3. Codex reproducer A: realistic fetch + 'Installing gnutls' + 'ERROR: gnutls: No space left on device' -> rejected!
    proc, repos, outcome_dir, _ = run_case(
        "gnutls_disk_full",
        f"""
{apk_progress}
echo "ERROR: gnutls-3.8.13-r0: No space left on device" >&2
exit 1
""",
    )
    assert proc.returncode == 1
    assert "dl-cdn.alpinelinux.org" in repos.read_text()
    assert not (outcome_dir / "infra-skipped.json").exists()
    assert json.loads((outcome_dir / "outcome.json").read_text())["status"] == "failed"

    # 4. Codex reproducer B: fetch progress lines + 'unable to select packages' / 'no such package' -> rejected!
    proc, repos, outcome_dir, _ = run_case(
        "fetch_progress_plus_no_such_package",
        f"""
{apk_progress}
cat >&2 <<'EOF'
ERROR: unable to select packages:
  cassini-typo-pkg (no such package):
    required by: world[cassini-typo-pkg]
EOF
exit 2
""",
    )
    assert proc.returncode == 2
    assert "dl-cdn.alpinelinux.org" in repos.read_text()
    assert not (outcome_dir / "infra-skipped.json").exists()
    assert json.loads((outcome_dir / "outcome.json").read_text())["status"] == "failed"

    # 5. Same-attempt mixed failure (upstream ERROR line + disk-full ERROR line) -> rejected!
    proc, repos, outcome_dir, _ = run_case(
        "same_attempt_mixed_errors",
        f"""
{apk_progress}
echo "ERROR: https://dl-cdn.alpinelinux.org/alpine/edge/main: temporary error (try again later)" >&2
echo "ERROR: gnutls-3.8.13-r0: No space left on device" >&2
exit 1
""",
    )
    assert proc.returncode == 1
    assert "dl-cdn.alpinelinux.org" in repos.read_text()
    assert not (outcome_dir / "infra-skipped.json").exists()

    # 6. Mixed retries on primary: attempts 1-2 fail upstream, final retry 3 fails with disk full -> rejected!
    proc, repos, outcome_dir, _ = run_case(
        "mixed_retries_primary",
        f"""
count_file="{tmp_path}/mixed_retries_primary/attempt"
n=$(cat "$count_file" 2>/dev/null || echo 0)
n=$((n + 1))
echo "$n" > "$count_file"
{apk_progress}
if [ "$n" -lt 3 ]; then
  echo "ERROR: onetbb-2023.2.0-r0: v2 package integrity error" >&2
else
  echo "ERROR: gnutls-3.8.13-r0: No space left on device" >&2
fi
exit 1
""",
    )
    assert proc.returncode == 1
    assert "dl-cdn.alpinelinux.org" in repos.read_text()
    assert not (outcome_dir / "infra-skipped.json").exists()

    # 7. Mixed retries on fallback: primary fails upstream, fallback attempts 1-2 fail upstream, fallback attempt 3 fails non-upstream -> rejected!
    proc, repos, outcome_dir, _ = run_case(
        "mixed_retries_fallback",
        f"""
{apk_progress}
if grep -q 'dl-cdn.alpinelinux.org' "{tmp_path}/mixed_retries_fallback/repositories"; then
  echo "ERROR: onetbb-2023.2.0-r0: v2 package integrity error" >&2
  exit 1
fi
count_file="{tmp_path}/mixed_retries_fallback/fb_attempt"
n=$(cat "$count_file" 2>/dev/null || echo 0)
n=$((n + 1))
echo "$n" > "$count_file"
if [ "$n" -lt 3 ]; then
  echo "ERROR: https://mirrors.edge.kernel.org/alpine/edge/main: Operation timed out" >&2
  exit 1
fi
echo "ERROR: unable to select packages:" >&2
echo "  foo (no such package):" >&2
exit 2
""",
    )
    assert proc.returncode == 2
    assert "mirrors.edge.kernel.org" in repos.read_text()
    assert not (outcome_dir / "infra-skipped.json").exists()
    assert json.loads((outcome_dir / "outcome.json").read_text())["status"] == "failed"

    # 8. Failure with progress output and zero 'ERROR: ' lines -> rejected!
    proc, repos, outcome_dir, _ = run_case(
        "no_apk_error_line",
        f"""
{apk_progress}
echo "Killed" >&2
exit 137
""",
    )
    assert proc.returncode == 137
    assert "dl-cdn.alpinelinux.org" in repos.read_text()
    assert not (outcome_dir / "infra-skipped.json").exists()

    # 9. Attempts 1-2 hit upstream apk error, final retry 3 dies without an apk ERROR line (e.g. OOM Killed) ->
    # final retry is judged separately from earlier attempts and rejected!
    proc, repos, outcome_dir, _ = run_case(
        "earlier_upstream_then_final_oom_killed",
        f"""
count_file="{tmp_path}/earlier_upstream_then_final_oom_killed/attempt"
n=$(cat "$count_file" 2>/dev/null || echo 0)
n=$((n + 1))
echo "$n" > "$count_file"
{apk_progress}
if [ "$n" -lt 3 ]; then
  echo "ERROR: onetbb-2023.2.0-r0: v2 package integrity error" >&2
  exit 1
fi
echo "Killed" >&2
exit 137
""",
    )
    assert proc.returncode == 137
    assert "dl-cdn.alpinelinux.org" in repos.read_text()
    assert not (outcome_dir / "infra-skipped.json").exists()
