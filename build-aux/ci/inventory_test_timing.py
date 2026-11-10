#!/usr/bin/env python3
# Copyright 2026 Codemyriad OÜ
# SPDX-License-Identifier: GPL-3.0-or-later
"""D88 conservative timing-site inventory, not a linter or a proof of violations.

Scan tracked test sources, including the pinned protocol copy. Include clocks,
fixed delays, timer callbacks, bounded waits, and assertion candidates found by
clock-value propagation. Media timestamps, fake clocks and outer hang guards
need human classification; keep them in the inventory rather than hiding them.
"""

import ast
import collections
import csv
import pathlib
import re
import subprocess

root = pathlib.Path(__file__).resolve().parents[2]
paths = subprocess.check_output(["git", "ls-files"], cwd=root, text=True).splitlines()
clock = re.compile(
    r"\b(?:time\.(?:monotonic(?:_ns)?|perf_counter(?:_ns)?|time(?:_ns)?|clock_gettime(?:_ns)?)|GLib\.(?:get_monotonic_time|get_real_time)|(?:Date|performance)\.now|timeit\.(?:timeit|repeat|default_timer))\s*\("
)
sleep = re.compile(
    r"\b(?:time|asyncio)\.sleep\s*\(|\b(?:sleep|waitForTimeout)\s*\(|\bThread\.sleep\s*\(|\bsleep\s+[.0-9]"
)
timer = re.compile(r"\b(?:GLib\.(?:timeout_add(?:_seconds)?)|setTimeout|setInterval)\s*\(")
assert_words = re.compile(
    r"elapsed|exit_seconds|deadline|latency|heartbeat|perf_counter|monotonic|wall_time|wallTime|cpu_gaps|retry_gaps|frame_gaps",
    re.I,
)
rows = {}
files = []


def add(path, line, kind, source):
    key = (path, line, kind)
    rows[key] = [
        path,
        line,
        kind,
        "vendored" if path.startswith("browser-protocol/") else "gnome",
        "review",
        " ".join(source.split()),
    ]


for rel in paths:
    p = root / rel
    if not (
        rel.startswith("tests/")
        or ("/tests/" in rel)
        or re.search(r"(?:^|/)(?:test_[^/]+\.py|[^/]+\.test\.[cm]?js)$", rel)
    ):
        continue
    if p.suffix not in (".py", ".js", ".mjs", ".sh", ".kt", ".c", ".html"):
        continue
    files.append(rel)
    text = p.read_text(errors="replace")
    lines = text.splitlines()
    for i, line in enumerate(lines, 1):
        if clock.search(line):
            add(rel, i, "clock-read", line)
        if sleep.search(line):
            add(rel, i, "fixed-delay", line)
        if timer.search(line):
            add(rel, i, "timer-callback", line)
        if (
            re.search(r"\bassert(?:\.|\s|\().*", line)
            and assert_words.search(line)
            and re.search(r"[<>]|\b(?:assertLess|assertGreater)", line)
        ):
            add(rel, i, "temporal-assertion", line)
        if re.search(r"\.(?:wait|join|communicate|select)\(\s*(?:timeout\s*=\s*)?[0-9]|\btimeout\s*=", line):
            add(rel, i, "bounded-wait", line)
    if p.suffix != ".py":
        continue
    try:
        t = ast.parse(text)
    except SyntaxError:
        continue
    aliases = {}
    for n in ast.walk(t):
        if isinstance(n, ast.ImportFrom) and n.module in ("time", "asyncio"):
            for a in n.names:
                aliases[a.asname or a.name] = n.module + "." + a.name

    def names(n):
        return {x.id for x in ast.walk(n) if isinstance(x, ast.Name)}

    def direct(n, text=text, aliases=aliases):
        s = ast.get_source_segment(text, n) or ""
        return bool(
            clock.search(s)
            or any(
                k in names(n)
                and v.rsplit(".", 1)[-1]
                in ("monotonic", "monotonic_ns", "perf_counter", "perf_counter_ns", "time", "time_ns")
                for k, v in aliases.items()
            )
        )

    tainted = set()
    for _ in range(12):
        old = set(tainted)
        for n in ast.walk(t):
            if isinstance(n, (ast.Assign, ast.AnnAssign, ast.AugAssign)) and n.value is not None:
                target = n.targets if isinstance(n, ast.Assign) else [n.target]
                if direct(n.value) or names(n.value) & tainted:
                    for x in target:
                        tainted.update(names(x))
            if (
                isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and n.func.attr in ("append", "extend", "update")
                and any(direct(a) or names(a) & tainted for a in n.args)
            ):
                tainted.update(names(n.func.value))
        if old == tainted:
            break
    for n in ast.walk(t):
        if isinstance(n, ast.Assert) and (
            direct(n.test)
            or (
                names(n.test) & tainted
                and any(
                    isinstance(c, ast.Compare) and any(isinstance(o, (ast.Lt, ast.LtE, ast.Gt, ast.GtE)) for o in c.ops)
                    for c in ast.walk(n.test)
                )
            )
        ):
            add(rel, n.lineno, "temporal-assertion", ast.get_source_segment(text, n.test) or lines[n.lineno - 1])
        if isinstance(n, (ast.While, ast.If)) and direct(n.test):
            add(rel, n.lineno, "clock-condition", ast.get_source_segment(text, n.test) or lines[n.lineno - 1])
        if (
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and aliases.get(n.func.id) in ("time.sleep", "asyncio.sleep")
        ):
            add(rel, n.lineno, "fixed-delay", lines[n.lineno - 1])
output = root / "docs/test-timing-inventory.csv"
with output.open("w") as f:
    w = csv.writer(f, lineterminator="\n")
    w.writerow(["path", "line", "kind", "owner", "status", "expression"])
    w.writerows(rows[k] for k in sorted(rows))
print(
    "files scanned",
    len(files),
    "sites",
    len(rows),
    "affected files",
    len(set(r[0] for r in rows.values())),
    "kinds",
    dict(collections.Counter(r[2] for r in rows.values())),
)
print("owned", sum(r[3] == "gnome" for r in rows.values()), "vendored", sum(r[3] == "vendored" for r in rows.values()))
