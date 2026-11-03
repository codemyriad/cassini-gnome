#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Rebuild tests/shard-durations.json from pytest junit XML (G3).

    build-aux/ci/shard_durations.py JUNIT.xml... [--output tests/shard-durations.json]
    build-aux/ci/shard_durations.py JUNIT.xml... --report 4

Pass every shard's pytest-N.junit.xml of one run (Meson writes them to
meson-logs/; CI uploads them in the meson-logs-* artifacts), or one junit XML
of a whole-suite run with `-o junit_family=xunit1`. Each file's time is the sum
of its test cases' times. --report N prints the per-shard totals that
tests/shard_plan.py would give for N shards, without writing anything.
"""

import argparse
import json
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))
import shard_plan  # noqa: E402


def file_seconds(paths):
    seconds = defaultdict(float)
    for path in paths:
        for case in ET.parse(path).getroot().iter("testcase"):
            name = case.get("file")
            if not name:
                raise SystemExit(f"{path}: a test case has no file attribute; run pytest with -o junit_family=xunit1")
            # Relative to pytest's rootdir, the repository: tests/test_x.py.
            name = Path(name).as_posix().removeprefix("tests/")
            seconds[name] += float(case.get("time") or 0)
    return dict(seconds)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("junit", nargs="+", type=Path)
    parser.add_argument("--output", type=Path, default=shard_plan.TABLE)
    parser.add_argument("--report", type=int, metavar="N")
    args = parser.parse_args()
    seconds = file_seconds(args.junit)
    if args.report:
        table = {name: round(value, 1) for name, value in seconds.items()}
        shards = shard_plan.assign(sorted(table), args.report, table)
        for index in range(1, args.report + 1):
            mine = [name for name, shard in shards.items() if shard == index]
            print(f"shard {index}/{args.report}: {sum(table[n] for n in mine):6.1f} s, {len(mine)} files")
        return
    table = {name: round(seconds[name], 1) for name in sorted(seconds)}
    note = (
        "Seconds per test file (sum of its junit test-case times), for tests/shard_plan.py. "
        "Regenerate with build-aux/ci/shard_durations.py."
    )
    args.output.write_text(json.dumps({"note": note, "files": table}, indent=1) + "\n")


if __name__ == "__main__":
    main()
