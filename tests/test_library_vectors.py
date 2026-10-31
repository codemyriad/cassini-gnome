# SPDX-License-Identifier: GPL-3.0-or-later
"""Compare scanner output with independently hand-authored shared Library rows."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from cassini_talk.library import LibraryScanner

ROOT = Path(__file__).resolve().parents[1]
VECTORS = ROOT / "ux/library-vectors"
CASES = json.loads((VECTORS / "cases.json").read_text())


@pytest.fixture(scope="session")
def shared_library(tmp_path_factory):
    directory = tmp_path_factory.mktemp("shared-library") / "trees"
    subprocess.run([sys.executable, str(VECTORS / "make.py"), str(directory)], check=True)
    return directory


@pytest.mark.parametrize("case", CASES["cases"], ids=lambda c: c["id"])
def test_shared_library_vector(case, shared_library):
    directory = shared_library / case["id"]
    activity = json.loads((directory / "activity.json").read_text())
    actual = (
        LibraryScanner(directory / "root")
        .scan(capture_activity=activity)
        .as_dict(now_ms=CASES["nowMs"], tz=CASES["tz"])
    )
    expected = json.loads((VECTORS / "expected" / (case["id"] + ".json")).read_text())
    if artifact_root := os.environ.get("CASSINI_UX_ARTIFACTS"):
        output = Path(artifact_root) / "library-cases"
        output.mkdir(parents=True, exist_ok=True)
        (output / (case["id"] + ".json")).write_text(json.dumps(actual, ensure_ascii=False, indent=2) + "\n")
    assert actual == expected
