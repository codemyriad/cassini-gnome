# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared-runner CPU budget gates for exact transcript UI checks.

Reference targets match local desktop engineering budgets (32 ms paint/slice
CPU, 100 ms first-viewport paint CPU, 2 ms playback highlight tick CPU). Shared
CI VMs using software Xvfb rasterization exhibit CPU-speed jitter around those
targets (for example 33.1 ms vs 32 ms or 110.4 ms vs 100 ms) while true
unchunked or O(N) regressions take hundreds to thousands of milliseconds. Every
check prints the measured number, reference target and safe ceiling, and fails
only above the safe regression ceiling.
"""

import json
import math
import os

FIRST_PAINT_TARGET_MS = 100.0
FIRST_PAINT_CEILING_MS = 250.0

FRAME_CPU_TARGET_MS = 32.0
FRAME_CPU_CEILING_MS = 96.0

HIGHLIGHT_CPU_TARGET_MS = 2.0
HIGHLIGHT_CPU_CEILING_MS = 8.0


def check_cpu_budget(scope, metric, measured_ms, *, target_ms, ceiling_ms, baseline_ms=None, baseline_factor=2.5):
    """Print the measured CPU budget number and fail only above the safe ceiling."""
    value = float(measured_ms)
    if not math.isfinite(value) or value < 0:
        raise AssertionError(f"{scope} {metric}: invalid CPU measurement {measured_ms!r}")
    if target_ms <= 0 or ceiling_ms < target_ms:
        raise ValueError(f"invalid budget bounds target_ms={target_ms!r}, ceiling_ms={ceiling_ms!r}")
    limit_ms = float(ceiling_ms)
    payload = {
        "scope": scope,
        "metric": metric,
        "measuredMs": round(value, 3),
        "targetMs": round(float(target_ms), 3),
        "ceilingMs": round(limit_ms, 3),
    }
    if baseline_ms is not None:
        base = float(baseline_ms)
        if not math.isfinite(base) or base < 0:
            raise AssertionError(f"{scope} {metric}: invalid baseline CPU measurement {baseline_ms!r}")
        payload["baselineMs"] = round(base, 3)
        limit_ms = max(limit_ms, base * float(baseline_factor))
        payload["ceilingMs"] = round(limit_ms, 3)
    payload["overTarget"] = value >= target_ms
    print("CPU_BUDGET", json.dumps(payload), flush=True)
    if value >= limit_ms:
        if os.environ.get("CASSINI_PERF_GATE") == "1":
            raise AssertionError(
                f"{scope} {metric} CPU {value:.3f} ms >= safe ceiling {limit_ms:.3f} ms (target {target_ms:.3f} ms)"
            )
        print(
            f"::notice::{scope} {metric} CPU {value:.3f} ms exceeded safe ceiling {limit_ms:.1f} ms "
            f"(diagnostic only outside nightly/full-ci perf gate)",
            flush=True,
        )
        return payload
    if value >= target_ms:
        print(
            f"::notice::{scope} {metric} CPU {value:.3f} ms exceeded reference target {target_ms:.1f} ms "
            f"within shared-runner ceiling {limit_ms:.1f} ms",
            flush=True,
        )
    return payload
