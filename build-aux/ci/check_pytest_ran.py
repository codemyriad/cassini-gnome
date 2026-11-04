#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Require that a package build ran the whole pytest suite (G11, D20).

    build-aux/ci/check_pytest_ran.py "$CASSINI_TEST_LOGS"

The Source distro job of the same image leaves pytest to the package job, so
the package job proves from its own Meson logs that it ran it: every shard
pytest-1 .. pytest-N exactly once, each passed, each with the command
src/meson.build registers (no added selection such as -k, --deselect or a
narrower path), CASSINI_TEST_SHARD i/N, CASSINI_TEST_REQUIRE_GTK=1 and no
PYTEST_ADDOPTS; and each shard's junit file holds passing tests, none
skipped with the "also run(s) in GTK-enabled distro" fallback. Every
directory under the argument holding a testlog.json is checked, and there
must be at least one.
"""

import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FALLBACK = re.compile(r"also runs? in GTK-enabled distro")


def shard_count(meson_build=ROOT / "src/meson.build"):
    return int(re.search(r"^pytest_shards = (\d+)$", meson_build.read_text(), re.M)[1])


# The arguments src/meson.build gives each shard after the interpreter; change
# both together. The junit path and the tests directory are the build's own.
ARGUMENTS = ["-m", "pytest", "-q", "--durations=20", "-o", "junit_family=xunit1"]


def registered(command, shard):
    arguments = command[1:]
    return (
        len(arguments) == len(ARGUMENTS) + 2
        and arguments[: len(ARGUMENTS)] == ARGUMENTS
        and re.fullmatch(rf"--junitxml=/.+/meson-logs/pytest-{shard}\.junit\.xml", arguments[-2]) is not None
        and re.fullmatch(r"/.+/tests", arguments[-1]) is not None
    )


def problems(directory, shards):
    directory = Path(directory)
    entries = [json.loads(line) for line in (directory / "testlog.json").read_text().splitlines() if line.strip()]
    found = []
    runs = {}
    for entry in entries:
        name = entry["name"].rpartition(":")[2]
        if re.fullmatch(r"pytest-\d+", name):
            runs.setdefault(name, []).append(entry)
    expected = [f"pytest-{index}" for index in range(1, shards + 1)]
    if sorted(runs) != sorted(expected):
        found.append(f"{directory.name}: ran {sorted(runs)}, not {expected}")
    for index, name in enumerate(expected, 1):
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
        junit = directory / f"{name}.junit.xml"
        if not junit.is_file():
            found.append(f"{directory.name}: no {junit.name}")
            continue
        cases = list(ET.parse(junit).iter("testcase"))
        passed = [c for c in cases if not any(c.find(tag) is not None for tag in ("skipped", "failure", "error"))]
        bad = [c for c in cases if c.find("failure") is not None or c.find("error") is not None]
        fallback = [
            c for c in cases if (s := c.find("skipped")) is not None and FALLBACK.search(s.get("message") or "")
        ]
        if not passed:
            found.append(f"{directory.name}: {junit.name} has no passing test")
        if bad:
            found.append(f"{directory.name}: {junit.name} has {len(bad)} failed tests")
        if fallback:
            found.append(f"{directory.name}: {junit.name} skipped {len(fallback)} GTK tests the Source job left to it")
    return found


def main(argv=sys.argv[1:]):
    if len(argv) != 1:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    directories = sorted(path.parent for path in Path(argv[0]).rglob("testlog.json"))
    if not directories:
        print(f"::error::no Meson testlog.json under {argv[0]}: the package build ran no tests")
        return 1
    shards = shard_count()
    found = [problem for directory in directories for problem in problems(directory, shards)]
    for problem in found:
        print(f"::error::{problem}")
    if found:
        return 1
    print(f"pytest-1 .. pytest-{shards} ran and passed in: {', '.join(d.name for d in directories)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
