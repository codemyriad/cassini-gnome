#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Check the unchanged D36 numerical budgets against real 60-second reports.

Internal counters separate analysis/tick/snapshot from capture and the renderer.
Keep whole-process CPU figures alongside these: software-renderer measurements
cannot establish a hardware GSK/GPU drawing budget on their own.
"""

import argparse
import json
from pathlib import Path


def check(reports):
    failures = []
    cases = {report["case"]: report for report in reports}
    if len(reports) != 5 or set(cases) != set("ABCDE"):
        return ["Expected exactly one report for each case A, B, C, D and E"]
    hashes = {value.get("sourceSha256") for value in reports}
    if len(hashes) != 1 or None in hashes:
        failures.append("Reports must have the same production source digest")
    for case, value in cases.items():

        def bound(field, limit, *, value=value, case=case):
            actual = value.get(field)
            if not isinstance(actual, (int, float)) or not 0 <= actual <= limit:
                failures.append(f"{case}: {field}={actual!r}; expected 0..{limit}")

        if value["seconds"] < 60:
            failures.append(f"{case}: measurement was shorter than 60 seconds")
        bound("analysisCpuPercent", 0 if case in "CE" else (2 if case == "D" else 1))
        bound("renderStateBytes", 128 * 1024)
        bound("late", 0)
        draw = value["drawingTickPercent"] + value["snapshotPercent"]
        maximum = 0 if case in "CE" else (0.6 if case == "A" else 2)
        if draw > maximum:
            failures.append(f"{case}: tick + snapshot={draw:.6f}%; limit {maximum}%")
        if case in "BD":
            bound("frameP95Ms", 17.5)
            bound("frameMaxMs", 33)
        if case in "CE":
            bound("analysisCalls", 0)
            bound("analysed", 0)
            bound("windowCallbacks", 0)
    return failures


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reports", nargs="+", type=Path)
    args = parser.parse_args()
    reports = [json.loads(path.read_text()) for path in args.reports]
    failures = check(reports)
    print(json.dumps(dict(passed=not failures, failures=failures, reports=reports), indent=2))
    raise SystemExit(bool(failures))


if __name__ == "__main__":
    main()
