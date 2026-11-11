#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Prove which pytest tests ran where, and gate every skipped suite on it (G11, D20).

    check_pytest_ran.py LOGS --export DIR [--github-output FILE]
    check_pytest_ran.py --source LOGS [--install-outcome DIR] --slug SLUG --export DIR [--github-output FILE]
    check_pytest_ran.py --gate DIR

A Source distro job leaves pytest to the package job of the same image.

Package job (LOGS holds the one Meson log directory of its build): pytest-1 ..
pytest-N each ran once and passed, with the arguments src/meson.build
registers, its CASSINI_TEST_SHARD, CASSINI_TEST_REQUIRE_GTK=1 and no
PYTEST_ADDOPTS; each shard read pyproject.toml, deselected nothing and tested
a tests/ tree identical to this checkout (tests/conftest.py records these in
pytest-N.run.json); every test file of the checkout is in the junit of the
shard tests/shard_plan.py gives it; nothing failed and the only skips are in
SKIPS, one in MESON_RUNS only where that Meson test passed in the same log. It
exports the node ids it ran and the os-release of the container that ran them,
which the job uploads as pytest-ran-<slug>.

Source job (--source): exports its node ids when its own logs show all of
pytest-1 .. pytest-N run, as pytest-nodes-<slug>, or else a pytest-skipped-
<slug> marker, whatever made it skip; --github-output writes kind and slug.

Gate (--gate, the downloaded pytest-* artifacts, one directory each): require
the complete Source/package matrix, even if both a Source job and its package
job disappeared. Then every
pytest-skipped-<slug> has a pytest-ran-<slug> from a container of the same
distribution release, and every package job ran, for each test function,
at least as many cases as the fedora:latest Source job (functions, not node
ids: pytest releases escape long parameter ids differently): a test left out
only in package builds, by a deselection, a conditional definition or a
patch, is missing there.
"""

import argparse
import json
import re
import shutil
import sys
import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))
import shard_plan  # noqa: E402

# The arguments src/meson.build gives each shard after the interpreter; change
# both together. The junit path and the tests directory are the build's own.
ARGUMENTS = ["-m", "pytest", "-q", "--durations=20", "-o", "junit_family=xunit1"]
# Every skip a package build may report: node id -> why it cannot run there.
SKIPS = {
    **{
        f"tests.test_bounded_run::test_nothing_of_a_run_outlives_it[scope-{ending}]": "no systemd --user"
        for ending in ("done", "timeout", "stubborn", "interrupt")
    },
    **{
        f"tests.test_bounded_run::{name}": "no systemd --user"
        for name in (
            "test_a_process_that_leaves_the_scope_dies_too",
            "test_the_scope_stays_in_the_callers_slice",
            "test_a_scope_it_cannot_check_is_reported",
        )
    },
    "tests.test_ci_orchestrator::test_actual_git_rename_keeps_old_area_coverage": "no git in the build",
    "tests.test_library_v2_finalize::test_a_really_full_tmpfs_leaves_nothing_in_the_root": "needs a tiny tmpfs",
    "tests.test_ux_vectors::test_locale_clock_style_in_a_real_locale_process[en_GB.UTF-8-14:18]": "locale",
    "tests.test_ux_vectors::test_locale_clock_style_in_a_real_locale_process[en_US.UTF-8-2:18 PM]": "locale",
    "tests.test_browser_host::test_canonical_conformance": "Meson runs it",
}
# Skips of a test that runs exactly the command of a Meson test registered in
# the same build (the shard checks that): node id -> that Meson test, which
# must have passed in the same testlog.json. Recording's host pytest runs them.
MESON_RUNS = {
    "tests.test_browser_host::test_canonical_conformance": "browser-protocol-conformance",
}
# The Source job whose node ids every package job must have run: it keeps the
# whole suite, with GTK, outside any package build.
REFERENCE = "fedora-latest"
ALPINE_CANARY = "alpine-edge"
ARTIFACT_RE = re.compile(r"^pytest-(nodes|skipped|infra-skipped|ran)-(.+?)(?:-attempt-([1-9]\d*))?$")
# os-release fields that name a release; rolling distributions change
# VERSION_ID between two pulls of the same tag.
RELEASE = ("ID", "VERSION_CODENAME", "VERSION_ID")
ROLLING = {"arch", "opensuse-tumbleweed"}

# This complete matrix is mandatory, independently of optional area selection.
# tests pin it to the Source and package workflows, including Fedora's reference.
REQUIRED_ARTIFACTS = (
    "pytest-skipped-debian-testing",
    "pytest-skipped-ubuntu-26.04",
    "pytest-nodes-fedora-latest",
    "pytest-skipped-registry.opensuse.org-opensuse-tumbleweed-latest",
    "pytest-skipped-archlinux-latest",
    "pytest-nodes-alpine-edge",
    "pytest-ran-debian-testing",
    "pytest-ran-ubuntu-26.04",
    "pytest-ran-fedora-44",
    "pytest-ran-registry.opensuse.org-opensuse-tumbleweed-latest",
    "pytest-ran-archlinux-latest",
)


def shard_count(meson_build=ROOT / "src/meson.build"):
    return int(re.search(r"^pytest_shards = (\d+)$", meson_build.read_text(), re.M)[1])


def patterns(pyproject=ROOT / "pyproject.toml"):
    options = tomllib.loads(pyproject.read_text()).get("tool", {}).get("pytest", {}).get("ini_options", {})
    value = options.get("python_files", shard_plan.PATTERNS)
    return tuple(value.split() if isinstance(value, str) else value)


def expected_files(shards, tests=ROOT / "tests"):
    """{shard: {"tests/<file>"}} for this checkout."""
    assignment = shard_plan.assign(shard_plan.test_files(tests, patterns()), shards)
    expected = {index: set() for index in range(1, shards + 1)}
    for name, index in assignment.items():
        expected[index].add("tests/" + name)
    return expected


def registered(command, shard):
    arguments = command[1:]
    return (
        len(arguments) == len(ARGUMENTS) + 2
        and arguments[: len(ARGUMENTS)] == ARGUMENTS
        and re.fullmatch(rf"--junitxml=/.+/meson-logs/pytest-{shard}\.junit\.xml", arguments[-2]) is not None
        and re.fullmatch(r"/.+/tests", arguments[-1]) is not None
    )


def node(case):
    return f"{case.get('classname')}::{case.get('name')}"


def status(case):
    for tag in ("failure", "error", "skipped"):
        if case.find(tag) is not None:
            return tag
    return "passed"


def os_release_text(text):
    pairs = (line.partition("=") for line in text.splitlines() if "=" in line)
    return {key: value.strip().strip("\"'") for key, _, value in pairs}


def os_release(path):
    return os_release_text(Path(path).read_text()) if Path(path).is_file() else {}


def release(values):
    keys = RELEASE[:2] if values.get("ID") in ROLLING else RELEASE
    return {key: values.get(key) for key in keys}


def shard_runs(directory, shards):
    """{pytest-i: [testlog.json entries]} from one Meson log directory."""
    runs = {}
    for line in (Path(directory) / "testlog.json").read_text().splitlines():
        if line.strip() and re.fullmatch(r"pytest-\d+", (entry := json.loads(line))["name"].rpartition(":")[2]):
            runs.setdefault(entry["name"].rpartition(":")[2], []).append(entry)
    return runs


def meson_results(directory):
    """{Meson test name: [results]} from one Meson log directory."""
    results = {}
    for line in (Path(directory) / "testlog.json").read_text().splitlines():
        if line.strip():
            entry = json.loads(line)
            results.setdefault(entry["name"].rpartition(":")[2], []).append(entry.get("result"))
    return results


def nodes(directory, shards):
    """{node id: status} over every shard's junit."""
    found = {}
    for index in range(1, shards + 1):
        junit = Path(directory) / f"pytest-{index}.junit.xml"
        if junit.is_file():
            found.update({node(case): status(case) for case in ET.parse(junit).iter("testcase")})
    return found


def problems(directory, shards, expected=None, tests=None):
    directory = Path(directory)
    expected = expected_files(shards) if expected is None else expected
    tests = shard_plan.tests_digest(ROOT / "tests") if tests is None else tests
    found = []
    runs = shard_runs(directory, shards)
    names = [f"pytest-{index}" for index in range(1, shards + 1)]
    if sorted(runs) != sorted(names):
        found.append(f"{directory.name}: ran {sorted(runs)}, not {names}")
    if not os_release(directory / "os-release").get("ID"):
        found.append(f"{directory.name}: no os-release from the container that ran the tests")
    for index, name in enumerate(names, 1):
        if len(runs.get(name, [])) != 1:
            if name in runs:
                found.append(f"{directory.name}: {name} ran {len(runs[name])} times")
            continue
        entry = runs[name][0]
        if entry.get("result") != "OK":
            found.append(f"{directory.name}: {name} result {entry.get('result')}")
        command = entry.get("command") or []
        if not registered(command, index):
            found.append(f"{directory.name}: {name} ran {command}, not the registered command")
        env = entry.get("env") or {}
        if env.get("CASSINI_TEST_SHARD") != f"{index}/{shards}":
            found.append(f"{directory.name}: {name} ran shard {env.get('CASSINI_TEST_SHARD')}, not {index}/{shards}")
        if env.get("CASSINI_TEST_REQUIRE_GTK") != "1":
            found.append(f"{directory.name}: {name} ran without CASSINI_TEST_REQUIRE_GTK=1")
        if "PYTEST_ADDOPTS" in env:
            found.append(f"{directory.name}: {name} ran with PYTEST_ADDOPTS={env['PYTEST_ADDOPTS']!r}")
        record = directory / f"{name}.run.json"
        run = json.loads(record.read_text()) if record.is_file() else {}
        if run.get("tests") != tests:
            found.append(f"{directory.name}: {name} tested a tests/ tree other than this checkout's")
        source = command[-1].removesuffix("/tests") if command else ""
        if run.get("inipath") != f"{source}/pyproject.toml":
            found.append(f"{directory.name}: {name} read {run.get('inipath')!r}, not the source's pyproject.toml")
        if run.get("deselected") != []:
            found.append(f"{directory.name}: {name} deselected {(run.get('deselected') or ['?'])[:5]}")
        junit = directory / f"{name}.junit.xml"
        if not junit.is_file():
            found.append(f"{directory.name}: no {junit.name}")
            continue
        cases = list(ET.parse(junit).iter("testcase"))
        files = {case.get("file") for case in cases}
        if missing := sorted(expected[index] - files):
            found.append(f"{directory.name}: {name} ran no test from {missing}")
        if extra := sorted(files - expected[index]):
            found.append(f"{directory.name}: {name} ran {extra}, which shard_plan does not give it")
        if failed := [node(c) for c in cases if status(c) in ("failure", "error")]:
            found.append(f"{directory.name}: {name} failed {failed[:5]}")
        if skipped := [node(c) for c in cases if status(c) == "skipped" and node(c) not in SKIPS]:
            found.append(f"{directory.name}: {name} skipped {skipped[:5]} (not in SKIPS)")
        for skipped in (node(c) for c in cases if status(c) == "skipped" and node(c) in MESON_RUNS):
            if meson_results(directory).get(MESON_RUNS[skipped]) != ["OK"]:
                found.append(f"{directory.name}: {name} skipped {skipped}, but {MESON_RUNS[skipped]} did not pass")
    return found


def export(directory, shards, out):
    """The node ids and os-release the gate compares, in out/."""
    out.mkdir(parents=True, exist_ok=True)
    listed = nodes(directory, shards)
    (out / "nodes.json").write_text(json.dumps(listed, indent=0, sort_keys=True) + "\n")
    if (directory / "os-release").is_file():
        shutil.copyfile(directory / "os-release", out / "os-release")
    return listed


def slug(directory):
    return re.sub(r"\.\d+$", "", re.sub(r"^(deb|rpm|arch|distro)-", "", Path(directory).name))


def infra_problems(image, path):
    path = Path(path)
    if image != ALPINE_CANARY:
        return [f"infrastructure skip is only allowed for {ALPINE_CANARY}, not {image}"]
    marker = path / "infra-skipped.json"
    if not marker.is_file():
        return [f"{image}: missing infra-skipped.json"]
    try:
        data = json.loads(marker.read_text())
    except ValueError:
        return [f"{image}: invalid infra-skipped.json"]
    if not isinstance(data, dict):
        return [f"{image}: invalid infra-skipped.json"]
    if (
        data.get("image") != "alpine:edge"
        or data.get("step") != "install-deps"
        or data.get("reason") != "apk-upstream-mirror"
    ):
        return [f"{image}: infrastructure skip only accepts alpine:edge install-deps apk-upstream-mirror, got {data!r}"]
    detail = data.get("detail")
    if not isinstance(detail, str) or not detail.startswith("ERROR: "):
        return [f"{image}: infrastructure skip missing upstream apk ERROR detail"]
    if os_release(path / "os-release").get("ID") != "alpine":
        return [f"{image}: infrastructure skip os-release is not alpine"]
    present = {entry.name for entry in path.iterdir()}
    if present != {"infra-skipped.json", "os-release"}:
        return [f"{image}: infrastructure skip cannot accompany unexpected files {sorted(present)}"]
    return []


def _paths_overlap(first, second):
    left, right = Path(first).resolve(), Path(second).resolve()
    return left == right or left.is_relative_to(right) or right.is_relative_to(left)


def resolve_artifacts(directory):
    """Select only the highest attempt's artifact per (producer, image)."""
    directory = Path(directory)
    selected, grouped, found = {}, {}, []
    if not directory.is_dir():
        return selected, found
    for path in sorted(p for p in directory.iterdir() if p.is_dir()):
        match = ARTIFACT_RE.match(path.name)
        if match is None:
            found.append(f"unexpected pytest artifact directory: {path.name}")
            continue
        kind, image, attempt_text = match.groups()
        attempt = int(attempt_text) if attempt_text else 0
        producer = "ran" if kind == "ran" else "source"
        grouped.setdefault((producer, image), []).append((attempt, kind, path))
    for (producer, image), entries in sorted(grouped.items()):
        latest = max(attempt for attempt, _, _ in entries)
        chosen = [(kind, path) for attempt, kind, path in entries if attempt == latest]
        if len(chosen) > 1:
            found.append(
                f"conflicting {producer} artifacts for {image} at attempt {latest}: {[p.name for _, p in chosen]}"
            )
            continue
        kind, path = chosen[0]
        selected[f"pytest-{kind}-{image}"] = path
    return selected, found


def source(logs, image, shards, out, *, install_outcome=None):
    """("nodes", "skipped" or "infra-skipped", slug) for a Source job, from its own logs."""
    logs_root = Path(logs) if logs else None
    if logs_root and install_outcome is not None and _paths_overlap(logs_root, install_outcome):
        raise ValueError(f"{image}: install-outcome {install_outcome} overlaps test-writable logs {logs}")
    if logs_root and logs_root.is_dir() and any(logs_root.rglob("infra-skipped.json")):
        raise ValueError(f"{image}: untrusted infra-skipped.json inside test-writable logs {logs}")
    outcome_dir = Path(install_outcome) / f"distro-{image}" if install_outcome is not None else None
    if outcome_dir is not None and image == ALPINE_CANARY:
        if not outcome_dir.is_dir():
            raise ValueError(f"{image}: missing trusted install outcome directory {outcome_dir}")
        if (outcome_dir / "infra-skipped.json").exists():
            if bad := infra_problems(image, outcome_dir):
                raise ValueError("; ".join(bad))
            image_logs = (
                [
                    p
                    for p in logs_root.iterdir()
                    if slug(p) == image or any(slug(c) == image for c in ([p] if p.is_file() else p.rglob("*")))
                ]
                if logs_root and logs_root.is_dir()
                else []
            )
            if image_logs:
                raise ValueError(
                    f"{image}: trusted install infra-skip cannot accompany test logs {[p.name for p in image_logs]}"
                )
            out.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(outcome_dir / "infra-skipped.json", out / "infra-skipped.json")
            shutil.copyfile(outcome_dir / "os-release", out / "os-release")
            return "infra-skipped", image
        outcome_file = outcome_dir / "outcome.json"
        if not outcome_file.is_file():
            raise ValueError(f"{image}: missing trusted install outcome in {outcome_dir}")
        try:
            outcome = json.loads(outcome_file.read_text())
        except ValueError as exc:
            raise ValueError(f"{image}: invalid trusted install outcome in {outcome_file}") from exc
        if (
            not isinstance(outcome, dict)
            or outcome.get("image") != "alpine:edge"
            or outcome.get("step") != "install-deps"
            or outcome.get("status") != "installed"
        ):
            raise ValueError(f"{image}: install stage did not complete normally: {outcome!r}")
    elif outcome_dir is not None and outcome_dir.exists():
        raise ValueError(f"{image}: unexpected install outcome directory {outcome_dir} for non-canary image")
    directories = (
        [p.parent for p in logs_root.rglob("testlog.json") if slug(p.parent) == image]
        if logs_root and logs_root.is_dir()
        else []
    )
    if len(directories) != 1:
        raise ValueError(
            f"{image}: {len(directories)} Meson testlog.json directories under {logs}, not 1 (job failed before meson test)"
        )
    runs = shard_runs(directories[0], shards)
    if failed := [
        f"{name}:{entry.get('result')}"
        for name, items in sorted(runs.items())
        for entry in items
        if entry.get("result") != "OK"
    ]:
        raise ValueError(f"{image}: failed pytest shard in Source logs: {failed}")
    out.mkdir(parents=True, exist_ok=True)
    ran = all(len(runs.get(f"pytest-{i}", [])) == 1 for i in range(1, shards + 1))
    if ran:
        export(directories[0], shards, out)
    else:
        if (directories[0] / "os-release").is_file():
            shutil.copyfile(directories[0] / "os-release", out / "os-release")
        (out / "skipped").write_text(f"{image}: pytest-1 .. pytest-{shards} did not all run\n")
    return ("nodes" if ran else "skipped"), image


def gate(directory):
    """Reject missing matrix jobs before comparing releases and test cases."""
    selected, found = resolve_artifacts(directory)
    if found:
        return found
    for name, path in sorted(selected.items()):
        if name.startswith("pytest-infra-skipped-"):
            found.extend(infra_problems(name.removeprefix("pytest-infra-skipped-"), path))
    if found:
        return found
    missing = []
    canary_infra = f"pytest-infra-skipped-{ALPINE_CANARY}"
    for name in REQUIRED_ARTIFACTS:
        if name == f"pytest-nodes-{ALPINE_CANARY}" and canary_infra in selected:
            continue
        record = "skipped" if name.startswith("pytest-skipped-") else "nodes.json"
        if name not in selected or not (selected[name] / record).is_file():
            missing.append(f"missing required execution proof: {name}/{record}")
    return missing or compare_proofs(directory, selected=selected)


def compare_proofs(directory, *, selected=None):
    """Compare the proofs present; gate also requires the complete matrix."""
    if selected is None:
        artifacts, found = resolve_artifacts(directory)
        if found:
            return found
    else:
        artifacts, found = selected, []

    def kind(prefix):
        return {name.removeprefix(prefix): path for name, path in artifacts.items() if name.startswith(prefix)}

    skipped, ran, sources, infra = (
        kind("pytest-skipped-"),
        kind("pytest-ran-"),
        kind("pytest-nodes-"),
        kind("pytest-infra-skipped-"),
    )
    for image, path in sorted(infra.items()):
        found.extend(infra_problems(image, path))
    for image, path in sorted(skipped.items()):
        if image not in ran:
            found.append(f"Source skipped pytest on {image} and no package job proved it ran there")
            continue
        wanted, got = release(os_release(path / "os-release")), release(os_release(ran[image] / "os-release"))
        if not wanted.get("ID") or wanted != got:
            found.append(f"{image}: Source ran in {wanted}, but the package job's tests ran in {got}")
    if ran and REFERENCE not in sources:
        found.append(f"no pytest-nodes-{REFERENCE}: the Source job that is the reference did not run pytest")
    elif ran:
        reference = functions(json.loads((sources[REFERENCE] / "nodes.json").read_text()))
        for image, path in sorted(ran.items()):
            listed = json.loads((path / "nodes.json").read_text()) if (path / "nodes.json").is_file() else {}
            cases = functions(listed)
            if missing := sorted(f"{k} ({cases.get(k, 0)}/{n})" for k, n in reference.items() if cases.get(k, 0) < n):
                found.append(f"{image}: the package job never ran {len(missing)} tests {REFERENCE} ran: {missing[:5]}")
            if bad := sorted(n for n, s in listed.items() if s != "passed" and not (s == "skipped" and n in SKIPS)):
                found.append(f"{image}: {bad[:5]} did not pass")
    return found


def functions(listed):
    """{classname::function: number of cases} from node ids."""
    counts = {}
    for name in listed:
        key = name.partition("[")[0]
        counts[key] = counts.get(key, 0) + 1
    return counts


def report(found):
    for problem in found:
        print(f"::error::{problem}")
    return 1 if found else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("logs", nargs="?", type=Path)
    parser.add_argument("--export", type=Path, help="write the node ids and os-release here")
    parser.add_argument("--github-output", type=Path)
    parser.add_argument("--source", action="store_true", help="LOGS are a Source distro job's")
    parser.add_argument("--install-outcome", type=Path, help="--source: host-only install outcome directory")
    parser.add_argument("--slug", help="--source: the image slug of this job's matrix image")
    parser.add_argument("--gate", type=Path, help="the downloaded pytest-* artifacts, one directory each")
    args = parser.parse_args(argv)
    if args.gate:
        found = gate(args.gate)
        if not found:
            selected, _ = resolve_artifacts(args.gate)
            canary_infra = selected.get(f"pytest-infra-skipped-{ALPINE_CANARY}")
            if canary_infra is not None:
                detail = json.loads((canary_infra / "infra-skipped.json").read_text())["detail"]
                print(f"::warning::Pytest coverage accepted alpine:edge install infrastructure skip: {detail}")
        return report(found)
    shards = shard_count()
    if args.source:
        if not args.slug or not args.export:
            parser.error("--source needs --slug and --export")
        try:
            kind, image = source(args.logs, args.slug, shards, args.export, install_outcome=args.install_outcome)
        except ValueError as exc:
            return report([str(exc)])
        if args.github_output:
            with args.github_output.open("a") as output:
                output.write(f"kind={kind}\nslug={image}\n")
        if kind == "nodes":
            print(f"{image}: ran pytest-1 .. pytest-{shards}")
        elif kind == "infra-skipped":
            print(f"::warning::{image}: upstream install infrastructure skip")
        else:
            print(f"{image}: skipped pytest")
        return 0
    if args.logs is None:
        parser.error("LOGS, --source or --gate is required")
    directories = sorted(path.parent for path in args.logs.rglob("testlog.json"))
    if len(directories) != 1:
        return report([f"{len(directories)} Meson testlog.json under {args.logs}, not one: {directories}"])
    if found := problems(directories[0], shards):
        return report(found)
    if args.export:
        export(directories[0], shards, args.export)
    if args.github_output:
        with args.github_output.open("a") as output:
            output.write(f"slug={slug(directories[0])}\n")
    print(f"pytest-1 .. pytest-{shards} ran every test file and passed in {directories[0].name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
