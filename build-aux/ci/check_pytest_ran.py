#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Prove that a package build ran the whole pytest suite (G11, D20).

    build-aux/ci/check_pytest_ran.py LOGS [--github-output FILE]
    build-aux/ci/check_pytest_ran.py --gate ARTIFACT-NAMES

A Source distro job leaves pytest to the package job of the same image, so
the package job proves from its own Meson logs (the one directory under LOGS
holding a testlog.json) what pytest ran:

- pytest-1 .. pytest-N each ran exactly once and passed, with the arguments
  src/meson.build registers (no -k, --deselect, -p or narrower path), its
  CASSINI_TEST_SHARD, CASSINI_TEST_REQUIRE_GTK=1 and no PYTEST_ADDOPTS;
- the shards' junit files together hold every test file of this checkout,
  each in the shard tests/shard_plan.py assigns it, so a file deleted,
  ignored or emptied in the build is missing here;
- nothing failed, and the only skips are those in SKIPS below, so a test
  that skips for want of a dependency or in a package build is refused.

With --github-output it writes `slug=<image slug>` (the log directory's name
without its deb-/rpm-/arch- prefix and .N suffix) for the job's
`pytest-ran-<slug>` marker artifact. --gate reads the run's artifact names
and requires a `pytest-ran-<slug>` for every `pytest-skipped-<slug>` that a
Source job uploads when it skips pytest.
"""

import argparse
import json
import re
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
    "tests/test_ci_orchestrator.py::test_actual_git_rename_keeps_old_area_coverage": "no git in the build",
    "tests/test_library_v2_finalize.py::test_a_really_full_tmpfs_leaves_nothing_in_the_root": "needs a tiny tmpfs",
    "tests/test_ux_vectors.py::test_locale_clock_style_in_a_real_locale_process[en_GB.UTF-8-14:18]": "locale",
    "tests/test_ux_vectors.py::test_locale_clock_style_in_a_real_locale_process[en_US.UTF-8-2:18 PM]": "locale",
}


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
    return f"{case.get('file')}::{case.get('name')}"


def problems(directory, shards, expected=None):
    directory = Path(directory)
    expected = expected_files(shards) if expected is None else expected
    entries = [json.loads(line) for line in (directory / "testlog.json").read_text().splitlines() if line.strip()]
    found = []
    runs = {}
    for entry in entries:
        name = entry["name"].rpartition(":")[2]
        if re.fullmatch(r"pytest-\d+", name):
            runs.setdefault(name, []).append(entry)
    names = [f"pytest-{index}" for index in range(1, shards + 1)]
    if sorted(runs) != sorted(names):
        found.append(f"{directory.name}: ran {sorted(runs)}, not {names}")
    for index, name in enumerate(names, 1):
        if len(runs.get(name, [])) != 1:
            if name in runs:
                found.append(f"{directory.name}: {name} ran {len(runs[name])} times")
            continue
        entry = runs[name][0]
        if entry.get("result") != "OK":
            found.append(f"{directory.name}: {name} result {entry.get('result')}")
        if not registered(entry.get("command") or [], index):
            found.append(f"{directory.name}: {name} ran {entry.get('command')}, not the registered command")
        env = entry.get("env") or {}
        if env.get("CASSINI_TEST_SHARD") != f"{index}/{shards}":
            found.append(f"{directory.name}: {name} ran shard {env.get('CASSINI_TEST_SHARD')}, not {index}/{shards}")
        if env.get("CASSINI_TEST_REQUIRE_GTK") != "1":
            found.append(f"{directory.name}: {name} ran without CASSINI_TEST_REQUIRE_GTK=1")
        if "PYTEST_ADDOPTS" in env:
            found.append(f"{directory.name}: {name} ran with PYTEST_ADDOPTS={env['PYTEST_ADDOPTS']!r}")
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
        if failed := [node(c) for c in cases if c.find("failure") is not None or c.find("error") is not None]:
            found.append(f"{directory.name}: {name} failed {failed[:5]}")
        if skipped := [node(c) for c in cases if c.find("skipped") is not None and node(c) not in SKIPS]:
            found.append(f"{directory.name}: {name} skipped {skipped[:5]} (not in SKIPS)")
    return found


def slug(directory):
    return re.sub(r"\.\d+$", "", re.sub(r"^(deb|rpm|arch)-", "", Path(directory).name))


def gate(names):
    skipped = {n.removeprefix("pytest-skipped-") for n in names if n.startswith("pytest-skipped-")}
    ran = {n.removeprefix("pytest-ran-") for n in names if n.startswith("pytest-ran-")}
    return [
        f"Source skipped pytest on {image} and no package job proved it ran there" for image in sorted(skipped - ran)
    ]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("logs", nargs="?", type=Path)
    parser.add_argument("--github-output", type=Path)
    parser.add_argument("--gate", type=Path, help="file with one artifact name per line")
    args = parser.parse_args(argv)
    if args.gate:
        found = gate(args.gate.read_text().split())
        for problem in found:
            print(f"::error::{problem}")
        return 1 if found else 0
    if args.logs is None:
        parser.error("LOGS or --gate is required")
    directories = sorted(path.parent for path in args.logs.rglob("testlog.json"))
    if len(directories) != 1:
        print(f"::error::{len(directories)} Meson testlog.json under {args.logs}, not one: {directories}")
        return 1
    shards = shard_count()
    found = problems(directories[0], shards)
    for problem in found:
        print(f"::error::{problem}")
    if found:
        return 1
    if args.github_output:
        with args.github_output.open("a") as output:
            output.write(f"slug={slug(directories[0])}\n")
    print(f"pytest-1 .. pytest-{shards} ran every test file and passed in {directories[0].name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
