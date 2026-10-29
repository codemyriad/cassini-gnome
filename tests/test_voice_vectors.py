# SPDX-License-Identifier: GPL-3.0-or-later
"""S-9 expected values were frozen independently against pinned Android code."""

import json
import runpy
from pathlib import Path

import pytest
from voice_vectors_support import run

ROOT = Path(__file__).parent / "ux/voices"
VECTORS = json.loads((ROOT / "vectors.json").read_bytes())
DESKTOP = json.loads((ROOT / "desktop-vectors.json").read_bytes())
compare = runpy.run_path(str(ROOT / "verify.py"))["compare"]


@pytest.mark.parametrize("case", VECTORS["cases"], ids=lambda c: c["id"])
def test_saved_voice_android_vector(case):
    actual = [run(c["operation"], c["input"]) for c in case["checks"]]
    expected = [c["expected"] for c in case["checks"]]
    compare(json.loads(json.dumps(actual)), expected, VECTORS["numberTolerance"], case["id"])


@pytest.mark.parametrize("case", DESKTOP["cases"], ids=lambda c: c["id"])
def test_saved_voice_desktop_vector(case):
    compare(json.loads(json.dumps(run(case["operation"], case["input"]))), case["expected"], 0, case["id"])
