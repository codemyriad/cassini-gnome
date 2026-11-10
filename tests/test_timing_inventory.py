# Copyright 2026 Codemyriad OÜ
# SPDX-License-Identifier: GPL-3.0-or-later
"""Inventory regression cases use source strings, never real clocks or sleeps."""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location("timing_inventory", ROOT / "build-aux/ci/inventory_test_timing.py")
inventory = importlib.util.module_from_spec(spec)
spec.loader.exec_module(inventory)


@pytest.mark.parametrize(
    "path",
    [
        "tests/ui/example.py",
        "browser-extension/tests/example.test.cjs",
        "browser-extension/tests/support/helper.cjs",
        "browser-protocol/extension/tests/helper.cjs",
        "browser-protocol/scenarios/worker-recovery.mjs",
        "browser-protocol/conformance/run.py",
        "browser-protocol/conformance/control/run.py",
    ],
)
def test_includes_test_drivers_and_both_pinned_extension_copies(path):
    assert inventory.test_source(path)
    rows = inventory.scan_text(path, "Date.now(); time.monotonic()")
    assert rows and all(row[3] == ("vendored" if path.startswith("browser-") else "gnome") for row in rows)


@pytest.mark.parametrize("path", ["src/runtime.py", "node_modules/foo.test.cjs", "tests/package.json"])
def test_excludes_non_test_sources(path):
    assert not inventory.test_source(path)


def kinds(source, suffix="py"):
    return {(row[1], row[2]) for row in inventory.scan_text("tests/example." + suffix, source)}


def test_python_attribute_aliases_import_aliases_defaults_and_tainted_conditions():
    source = """import time as t
from asyncio import sleep as nap
SLEEP = t.sleep
clock_alias = t.monotonic
later = clock_alias
SLEEP(0.001)
nap(0.001)
def until(clock=t.monotonic, *, pause=SLEEP):
    deadline = clock() + 5
    now = later()
    remaining = deadline - now
    if remaining <= 0:
        pause(0.1)
    while remaining > 0:
        assert remaining < 4
"""
    result = kinds(source)
    assert {
        (6, "fixed-delay"),
        (7, "fixed-delay"),
        (9, "clock-read"),
        (10, "clock-read"),
        (12, "clock-condition"),
        (13, "fixed-delay"),
        (14, "clock-condition"),
        (15, "temporal-assertion"),
    } <= result


@pytest.mark.parametrize(
    "clock",
    [
        "thread_time",
        "thread_time_ns",
        "process_time",
        "process_time_ns",
        "monotonic",
        "perf_counter",
        "time",
        "clock_gettime",
    ],
)
def test_python_cpu_and_wall_functions_and_imported_aliases(clock):
    result = kinds(f"from time import {clock} as read\nstart = read()\nassert read() - start < 1\n")
    assert {(2, "clock-read"), (3, "clock-read"), (3, "temporal-assertion")} <= result


def test_python_default_clock_aliases_and_long_dependency_chains():
    source = "import time\ndef probe(clock=time.monotonic):\n    v0 = clock()\n"
    source += "".join(f"    v{i} = v{i - 1} + 1\n" for i in range(1, 30))
    source += "    if v29 > 1:\n        assert v29 < 10\n"
    result = kinds(source)
    assert (33, "clock-condition") in result and (34, "temporal-assertion") in result


@pytest.mark.parametrize("suffix", ["js", "mjs", "cjs"])
def test_javascript_propagates_clocks_aliases_rates_and_loop_conditions(suffix):
    source = """const clock = Date.now;
const started = clock();
const wall = (Date.now() - started) / 1000;
const rate = audio / wall;
assert.ok(rate >= MIN_RATE);
const deadline = performance.now() + 500;
while (performance.now() < deadline) {}
const remaining = deadline - performance.now();
if (remaining <= 0) assert.ok(Date.now() - started < 1000);
await new Promise(resolve =>
  setTimeout(resolve, 10));
"""
    result = kinds(source, suffix)
    assert {
        (2, "clock-read"),
        (5, "temporal-assertion"),
        (7, "clock-condition"),
        (9, "clock-condition"),
        (9, "temporal-assertion"),
        (11, "fixed-delay"),
    } <= result


def test_javascript_multiline_containers_callbacks_iteration_and_same_line_assert():
    source = """const startEpoch = Date.now();
const continuity = tracks.map(track => {
  const wallSeconds = (Date.now()-startEpoch) / 1000;
  return {error: track.seconds-wallSeconds};
});
for (const measure of continuity) assert.ok(Math.abs(measure.error)<.25);
const stoppedAt = Date.now(); const withdrawalMs=Date.now()-stoppedAt;assert.ok(withdrawalMs<=5000);
const arrivals={};
arrivals[id].push([Date.now(), frames]);
const values=arrivals.tone;
values.push(Date.now()-startEpoch);
for (const delta of values) assert.ok(delta<300);
"""
    assert {(6, "temporal-assertion"), (7, "temporal-assertion"), (12, "temporal-assertion")} <= kinds(source, "mjs")


@pytest.mark.parametrize(
    "path,kind,needle",
    [
        ("browser-extension/tests/background-lifecycle.test.cjs", "temporal-assertion", "Date.now()-started>=250"),
        ("browser-protocol/tests/capture-real-time.mjs", "temporal-assertion", "rate >= MIN_RATE"),
        ("browser-protocol/tests/rtc-evidence-observations.mjs", "temporal-assertion", "withdrawalMs<=5000"),
        ("browser-protocol/tests/live-folder-driver.test.mjs", "temporal-assertion", "Date.now() - started < BOUND"),
        ("tests/recording/browser.mjs", "temporal-assertion", "measure.error"),
        ("tests/recording/browser.mjs", "temporal-assertion", "wallMutePosition)<.4"),
        ("tests/browser-setup/installed.mjs", "clock-condition", "while"),
        ("tests/ui/progress_wait.py", "clock-condition", "if now >= deadline"),
        ("tests/ui/companion-click.py", "clock-read", "time.thread_time()"),
        ("tests/ui/accessible-buttons.py", "clock-condition", "remaining <= 0"),
        ("tests/conftest.py", "clock-condition", "elapsed > budget / 2"),
    ],
)
def test_review_examples_are_present_in_real_repository(path, kind, needle):
    rows = inventory.scan_text(path, (ROOT / path).read_text())
    assert any(row[2] == kind and needle in row[5] for row in rows), (path, kind, needle)


def test_plain_numeric_checks_do_not_become_timing_assertions():
    for suffix in ("py", "mjs", "cjs"):
        source = "assert count < 5" if suffix == "py" else "const count=4; assert.ok(count<5);"
        assert not any(kind == "temporal-assertion" for _, kind in kinds(source, suffix))


def test_python_imported_glib_alias_and_reversed_alias_chain():
    source = """from gi.repository import GLib as gl
clock3 = clock2
clock2 = clock1
clock1 = gl.get_monotonic_time
start = clock3()
assert gl.get_monotonic_time() - start < 100
"""
    assert {(5, "clock-read"), (6, "clock-read"), (6, "temporal-assertion")} <= kinds(source)


def test_python_timed_fields_do_not_taint_unrelated_fields():
    source = """import time
self.sample = time.monotonic()
assert self.sample < 5
assert self.count < 5
item = self.sample
assert item < 5
"""
    result = kinds(source)
    assert {(3, "temporal-assertion"), (6, "temporal-assertion")} <= result
    assert (4, "temporal-assertion") not in result


def test_javascript_destructuring_indexed_updates_and_chained_function_aliases():
    source = """const read = Date.now;
const clock = read;
const {deadline} = {deadline: clock()+200};
let [now] = [clock()];
while (now < deadline) assert.ok(now <= deadline);
const arrivals={};
arrivals[id]=[clock()];
for (const [stamp] of arrivals) assert.ok(stamp<deadline);
"""
    assert {
        (3, "clock-read"),
        (4, "clock-read"),
        (5, "clock-condition"),
        (5, "temporal-assertion"),
        (8, "temporal-assertion"),
    } <= kinds(source, "cjs")
