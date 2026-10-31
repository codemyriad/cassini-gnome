# SPDX-License-Identifier: GPL-3.0-or-later
"""S-12 §2: the shared Library v2 name vectors (ux-draft/library-v2, vendored and pinned)."""

import hashlib
import json
from pathlib import Path

import pytest

from cassini_talk.library_v2 import names
from cassini_talk.strings import text

ROOT = Path(__file__).parents[1]
HERE = Path(__file__).parent / "ux"
VECTORS = json.loads((HERE / "library-v2/name-vectors.json").read_text(encoding="utf-8"))
MALFORMED = json.loads((HERE / "library-v2/name-vectors-malformed.json").read_text(encoding="utf-8"))


def test_vendored_library_v2_fixtures_match_their_pinned_upstream_bytes():
    upstream = json.loads((HERE / "UPSTREAM.json").read_text())
    pinned = [k for k in upstream["sha256"] if k.startswith("library-v2/")]
    assert sorted(pinned) == [
        "library-v2/name-vectors-malformed.json",
        "library-v2/name-vectors.json",
        "library-v2/x-schema.json",
    ]
    for name in pinned:
        data = (ROOT / upstream["paths"][name]).read_bytes()
        assert hashlib.sha256(data).hexdigest() == upstream["sha256"][name], name
        assert upstream["additionalSources"][name]["commit"].startswith("1bed4bc")


def test_the_untitled_fallback_is_the_catalog_string():
    assert VECTORS["untitled"]["value"] == MALFORMED["untitled"]["value"] == text("library.untitled")
    assert VECTORS["maxUtf8Bytes"] == names.MAX_BYTES == 200


def check(case, title):
    expected = case["expected"]
    prefix, offset, source = names.prefix_for(case["input"]["start"])
    name, clean = names.file_name(
        prefix, title, VECTORS["untitled"]["value"], case["input"]["existing"], case["input"]["ownName"]
    )
    assert (prefix, offset, source) == (
        expected["namePrefix"],
        expected["utcOffsetMinutes"],
        expected["namePrefixSource"],
    )
    assert clean == expected["sanitizedTitle"]
    assert name == expected["fileName"]
    assert len(name.encode()) == expected["fileNameUtf8Bytes"] <= 200


@pytest.mark.parametrize("case", VECTORS["cases"], ids=lambda c: c["id"])
def test_shared_name_vector(case):
    check(case, case["input"]["title"])


@pytest.mark.parametrize("case", MALFORMED["cases"], ids=lambda c: c["id"])
def test_shared_malformed_vector_is_accepted_and_sanitized(case):
    # Python accepts lone surrogates at this boundary, so it must give the shared name.
    check(case, names.decode_utf16_units(case["input"]["titleUtf16"]))


def test_vector_counts_and_coverage():
    ids = {c["id"] for c in VECTORS["cases"]}
    assert len(VECTORS["cases"]) == 164 and len(MALFORMED["cases"]) == 6
    for needed in ("basic", "collision-200-bytes-plus-suffix", "collision-sharp-s", "single-cluster-over-budget"):
        assert needed in ids
