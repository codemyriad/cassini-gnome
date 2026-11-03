# SPDX-License-Identifier: GPL-3.0-or-later
"""Which pytest shard runs which test file (G3).

Meson runs the suite as pytest-1 .. pytest-N, one after another, each with its
own timeout. The shards are balanced by file, from the measured durations in
tests/shard-durations.json: the known files go longest first to the least
loaded shard (ties to the lowest shard), then files the table does not list
yet go, in name order, the same way with the table's median duration. A known
file's shard depends only on the table, so adding or removing a test file never
moves another known file. Re-measure and update the table when shards drift
(the CI meson-logs artifacts carry each file's time in the pytest junit XML).
"""

import json
import statistics
from pathlib import Path

TABLE = Path(__file__).with_name("shard-durations.json")


def durations(table=TABLE):
    return {name: float(seconds) for name, seconds in json.loads(table.read_text())["files"].items()}


def assign(files, count, table=None):
    """Map every name in `files` (paths relative to tests/) to a shard, 1..count."""
    if count < 1:
        raise ValueError("at least one shard")
    table = durations() if table is None else table
    default = statistics.median(table.values()) if table else 1.0
    loads = [0.0] * count
    shards = {}
    for name in [*sorted(table, key=lambda n: (-table[n], n)), *sorted(set(files) - set(table))]:
        index = min(range(count), key=lambda i: (loads[i], i))
        loads[index] += table.get(name, default)
        shards[name] = index + 1
    return {name: shards[name] for name in files}


def parse(value):
    """'2/4' -> (2, 4)."""
    index, _, count = value.partition("/")
    index, count = int(index), int(count)
    if not 1 <= index <= count:
        raise ValueError(f"shard {value!r} is not i/N with 1 <= i <= N")
    return index, count
