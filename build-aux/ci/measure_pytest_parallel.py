#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""D68/R91 A/B: same full Meson suite, serial shards versus two workers.

Durations are observations, never pass conditions. Stop on the first failure or
coverage difference; this is an experiment, not a retry mechanism. Use a fresh
build directory and an outer job timeout as the hang guard.
"""

import argparse
import json
import subprocess
import time
import xml.etree.ElementTree as ET
from pathlib import Path


def run(command, log):
    with log.open("w") as output:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for line in process.stdout:
            print(line, end="", flush=True)
            output.write(line)
        if process.wait():
            raise subprocess.CalledProcessError(process.returncode, command)


def cases(build):
    result = {}
    for index in range(1, 5):
        path = build / "meson-logs" / f"pytest-{index}.junit.xml"
        for case in ET.parse(path).iter("testcase"):
            key = (case.get("classname"), case.get("name"))
            assert key not in result, f"duplicate test: {key}"
            assert case.find("failure") is None and case.find("error") is None, key
            result[key] = "skipped" if case.find("skipped") is not None else "passed"
    assert result, "empty pytest proof"
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pairs", type=int, default=10)
    parser.add_argument("--build", type=Path, default=Path("_build-ab"))
    parser.add_argument("--output", type=Path, default=Path("pytest-ab"))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    run(["meson", "setup", str(args.build)], args.output / "setup.log")
    run(["meson", "compile", "-C", str(args.build), "-j", "2"], args.output / "compile.log")
    measurements = []
    expected = None
    for pair in range(1, args.pairs + 1):
        # Alternate order to expose warm-cache/order effects.
        for parallel in (False, True) if pair % 2 else (True, False):
            name = f"{pair:02d}-" + ("two-workers" if parallel else "serial")
            run(
                ["meson", "configure", str(args.build), f"-Dpytest_parallel={str(parallel).lower()}"],
                args.output / f"{name}-configure.log",
            )
            print(f"Starting {name}: complete Meson suite, two worker slots", flush=True)
            started = time.monotonic()
            run(
                ["meson", "test", "-C", str(args.build), "--num-processes=2", "--maxfail=1", "--print-errorlogs"],
                args.output / f"{name}.log",
            )
            elapsed = time.monotonic() - started
            registered = json.loads((args.build / "meson-info/intro-tests.json").read_text())
            shards = [test for test in registered if test["name"].startswith("pytest-")]
            assert {test["name"] for test in shards} == {f"pytest-{i}" for i in range(1, 5)}
            assert all(test["is_parallel"] == parallel for test in shards)
            observed = cases(args.build)
            if expected is None:
                expected = observed
            assert observed == expected, f"{name}: test identities/results changed"
            # Retain every shard and native Meson result, not just aggregate times.
            import shutil

            shutil.copytree(args.build / "meson-logs", args.output / name)
            measurements.append(
                dict(
                    pair=pair,
                    parallel=parallel,
                    seconds=elapsed,
                    passed=sum(v == "passed" for v in observed.values()),
                    skipped=sum(v == "skipped" for v in observed.values()),
                )
            )
            (args.output / "measurements.json").write_text(json.dumps(measurements, indent=2) + "\n")
            print(json.dumps(measurements[-1]), flush=True)


if __name__ == "__main__":
    main()
