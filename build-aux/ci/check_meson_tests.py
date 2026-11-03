#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Check that a configured build registered every Meson test (G3).

    meson introspect BUILD --tests > tests.json
    build-aux/ci/check_meson_tests.py tests.json

build-aux/check-distros.sh runs this after `meson setup`: a test whose tool
is missing is silently left out of the build, and the run would then pass
without it. Reads the introspection JSON, not `meson test --list`, whose text
changes between Meson releases ("cassini-talk:pytest / pytest-1" in 1.7,
"pytest - cassini-talk:pytest-1" in 1.10). Each entry has a "name" and a
"suite" list of "project:suite" strings (just "project" for a test in no suite),
the same from the oldest Meson meson.build allows. The pytest suite must hold
exactly pytest-1 .. pytest-N, N being pytest_shards in src/meson.build, and
pytest-i must run shard i/N (its "env" CASSINI_TEST_SHARD): a name alone does
not say which shard a test runs, and a duplicated index leaves another unrun.
"""

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# Tests in no particular suite whose tool install-deps.sh must have installed.
REQUIRED = (
    "browser-protocol-conformance",
    "browser-control-conformance",
    "Validate desktop file",
    "Validate appstream file",
    "Validate schema file",
)


def shard_count(meson_build=ROOT / "src/meson.build"):
    return int(re.search(r"^pytest_shards = (\d+)$", meson_build.read_text(), re.M)[1])


def suites(test):
    """{"suite": ["cassini-talk:pytest"]} -> {"pytest"}; no suite -> set()."""
    return {entry.partition(":")[2] for entry in test["suite"] if ":" in entry}


def problems(tests, shards):
    names = {test["name"] for test in tests}
    pytest = sorted(test["name"] for test in tests if "pytest" in suites(test))
    expected = sorted(f"pytest-{index}" for index in range(1, shards + 1))
    found = []
    if pytest != expected:
        found.append(f"suite pytest has {pytest}, not {expected} (pytest_shards in src/meson.build)")
    for test in tests:
        if "pytest" in suites(test) and (match := re.fullmatch(r"pytest-(\d+)", test["name"])):
            shard, want = (test.get("env") or {}).get("CASSINI_TEST_SHARD"), f"{int(match[1])}/{shards}"
            if shard != want:
                found.append(f"{test['name']} runs CASSINI_TEST_SHARD={shard}, not {want}")
    found += [f"missing test (tool not installed?): {name}" for name in REQUIRED if name not in names]
    return found


def main(argv):
    if len(argv) != 2:
        sys.exit(f"usage: {argv[0]} INTROSPECT-TESTS.json")
    tests = json.loads(Path(argv[1]).read_text())
    for test in tests:
        print(f"{test['name']}  [{', '.join(test['suite'])}]")
    found = problems(tests, shard_count())
    for line in found:
        print(line, file=sys.stderr)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
