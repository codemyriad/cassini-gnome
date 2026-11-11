# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for shared-runner exact transcript CPU budget checks."""

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_budgets():
    spec = importlib.util.spec_from_file_location("exact_budgets", ROOT / "tests/ui/exact_budgets.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cpu_budget_prints_numbers_and_tolerates_shared_runner_jitter_within_safe_ceiling(capsys):
    budgets = load_budgets()

    # Within local desktop target: passes without notice and prints structured numbers.
    under = budgets.check_cpu_budget(
        "60min-diarized",
        "openWorstFrameCpuMs",
        21.4567,
        target_ms=budgets.FRAME_CPU_TARGET_MS,
        ceiling_ms=budgets.FRAME_CPU_CEILING_MS,
    )
    out = capsys.readouterr().out
    assert under == {
        "scope": "60min-diarized",
        "metric": "openWorstFrameCpuMs",
        "measuredMs": 21.457,
        "targetMs": 32.0,
        "ceilingMs": 96.0,
        "overTarget": False,
    }
    assert f"CPU_BUDGET {json.dumps(under)}" in out
    assert "::notice::" not in out

    # Shared-VM jitter (#373 run 43008177895: 33.101 ms vs 32 ms target, and 110.413 ms vs 100 ms first paint):
    # passes, prints the measured value and emits a visible notice.
    jitter = budgets.check_cpu_budget(
        "60min-diarized",
        "openWorstFrameCpuMs",
        33.101333,
        target_ms=budgets.FRAME_CPU_TARGET_MS,
        ceiling_ms=budgets.FRAME_CPU_CEILING_MS,
    )
    jitter_out = capsys.readouterr().out
    assert jitter["overTarget"] is True
    assert jitter["measuredMs"] == 33.101
    assert "CPU_BUDGET" in jitter_out
    assert "::notice::60min-diarized openWorstFrameCpuMs CPU 33.101 ms exceeded reference target 32.0 ms" in jitter_out

    first_paint_jitter = budgets.check_cpu_budget(
        "60min-diarized",
        "openFirstPaintCpuMs",
        110.413198,
        target_ms=budgets.FIRST_PAINT_TARGET_MS,
        ceiling_ms=budgets.FIRST_PAINT_CEILING_MS,
    )
    assert first_paint_jitter["overTarget"] is True
    assert "::notice::60min-diarized openFirstPaintCpuMs CPU 110.413 ms" in capsys.readouterr().out


def test_cpu_budget_fails_above_safe_regression_ceiling_and_supports_per_runner_baseline(capsys):
    budgets = load_budgets()

    with pytest.raises(AssertionError, match=">= safe ceiling 96.000 ms"):
        budgets.check_cpu_budget(
            "4h-diarized",
            "openWorstFrameCpuMs",
            96.0,
            target_ms=budgets.FRAME_CPU_TARGET_MS,
            ceiling_ms=budgets.FRAME_CPU_CEILING_MS,
        )
    assert '"measuredMs": 96.0' in capsys.readouterr().out

    # Optional per-runner baseline scales ceiling only when baseline * factor > static ceiling.
    scaled = budgets.check_cpu_budget(
        "60min-diarized",
        "openWorstFrameCpuMs",
        105.0,
        target_ms=budgets.FRAME_CPU_TARGET_MS,
        ceiling_ms=budgets.FRAME_CPU_CEILING_MS,
        baseline_ms=50.0,
        baseline_factor=2.5,
    )
    assert scaled["baselineMs"] == 50.0
    assert scaled["ceilingMs"] == 125.0
    capsys.readouterr()

    with pytest.raises(AssertionError, match=">= safe ceiling 125.000 ms"):
        budgets.check_cpu_budget(
            "60min-diarized",
            "openWorstFrameCpuMs",
            126.0,
            target_ms=budgets.FRAME_CPU_TARGET_MS,
            ceiling_ms=budgets.FRAME_CPU_CEILING_MS,
            baseline_ms=50.0,
            baseline_factor=2.5,
        )


def test_cpu_budget_rejects_invalid_measurements_and_bounds():
    budgets = load_budgets()
    for bad in (-0.1, float("nan"), float("inf")):
        with pytest.raises(AssertionError, match="invalid CPU measurement"):
            budgets.check_cpu_budget("scope", "metric", bad, target_ms=32.0, ceiling_ms=96.0)
    with pytest.raises(ValueError, match="invalid budget bounds"):
        budgets.check_cpu_budget("scope", "metric", 10.0, target_ms=100.0, ceiling_ms=32.0)


def test_exact_transcript_ui_tests_keep_long_turn_fixture_and_use_shared_budget_helper():
    from transcript_long_turn import long_turn_words

    words = long_turn_words()
    assert len(words) == 72899
    assert sum(word.speaker == "person-1" for word in words) == 899
    for rel in ("tests/ui/exact-transcript.py", "tests/ui/exact_long_turn.py", "tests/ui/exact_reader_long.py"):
        text = (ROOT / rel).read_text()
        assert "check_cpu_budget(" in text, rel
        assert "< 32" not in text and "< 100" not in text, rel
