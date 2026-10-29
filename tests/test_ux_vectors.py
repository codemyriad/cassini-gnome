# SPDX-License-Identifier: GPL-3.0-or-later
"""Run the coordinator's golden files without rewriting or normalising their bytes."""

import hashlib
import json
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

from cassini_viewer import ux_formats as fmt
from cassini_viewer.meeting_state import MeetingState

ROOT = Path(__file__).resolve().parents[1]
FORMATS = json.loads((ROOT / "tests/ux/format-vectors.json").read_bytes())
STATES = json.loads((ROOT / "tests/ux/states-vectors.json").read_bytes())
CASES = [
    (kind, case) for kind, cases in FORMATS.items() if isinstance(cases, list) and kind != "changelog" for case in cases
]


@pytest.mark.parametrize("kind,case", CASES, ids=[f"{kind}-{i}" for i, (kind, _) in enumerate(CASES)])
def test_format_vector(kind, case):
    context = dict(now_ms=FORMATS["nowMs"], tz=FORMATS["tz"])
    timers = {
        "position": fmt.position,
        "liveBadge": fmt.live_badge,
        "liveBig": fmt.live_big,
        "meetingDuration": fmt.meeting_duration,
        "talkTime": fmt.talk_time,
        "confirmedSpeech": fmt.confirmed_speech,
    }
    if kind in timers:
        actual = timers[kind](case["ms"])
    elif kind == "dayGroup":
        actual = fmt.day_group(case["atMs"], **context)
    elif kind == "rowTime":
        actual = fmt.row_time(case["atMs"], tz=FORMATS["tz"])
    elif kind == "previewSubtitle":
        actual = fmt.preview_subtitle(case["atMs"], case["durationMs"], received=case["received"], **context)
    elif kind == "searchDate":
        actual = fmt.search_date(case["atMs"], **context)
    elif kind == "searchContext":
        actual = fmt.search_context(case["atMs"], case["durationMs"], case["positionMs"], **context)
    elif kind == "rowMeta":
        actual = fmt.row_meta(case["durationMs"], case["names"])
    elif kind == "eta":
        actual = fmt.eta(case["minutes"])
    elif kind == "searchCount":
        actual = fmt.search_count(case["places"], case["meetings"])
    elif kind == "size":
        actual = fmt.size(case["bytes"], approx=case["approx"])
    else:
        pytest.fail(f"Golden vector group has no implementation: {kind}")
    assert actual == case["out"]


@pytest.mark.parametrize("case", STATES["vectors"], ids=lambda c: c["id"])
def test_state_vector(case):
    state = MeetingState.from_fields(case["fields"])
    badge = state.badge(live_ms=case["fields"].get("liveMs", 0))
    assert (badge.as_dict() if badge else None) == case["badge"]
    if "canWriteDown" in case:
        assert state.can_write_down is case["canWriteDown"]


def test_golden_bytes():
    subprocess.run([sys.executable, str(ROOT / "build-aux/check-ux-vectors.py")], check=True)


@pytest.mark.parametrize("change", ["older-copy", "published-addition", "duplicate"])
def test_selective_catalog_adoption_cannot_change_older_or_approved_copy(change):
    validator = runpy.run_path(str(ROOT / "build-aux/check-ux-vectors.py"))["catalog_additions"]
    base = dict(entries=[dict(id="older", en="Existing approved text")])
    entries = [dict(id="addition", en="Newly published text")]
    adoption = dict(
        ids=["addition"],
        baseSha256=hashlib.sha256((json.dumps(base, indent=1, ensure_ascii=False) + "\n").encode()).hexdigest(),
        entriesSha256=hashlib.sha256(
            json.dumps(entries, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
        ).hexdigest(),
    )
    value = dict(entries=base["entries"] + entries)
    assert validator(json.dumps(value), adoption) == entries
    if change == "duplicate":
        value["entries"].append(next(e for e in value["entries"] if e["id"] in adoption["ids"]))
    else:
        entry = next(e for e in value["entries"] if (e["id"] in adoption["ids"]) == (change == "published-addition"))
        entry["en"] += " unauthorised copy"
    with pytest.raises(ValueError):
        validator(json.dumps(value), adoption)


def test_calendar_uses_local_dates_across_dst_and_year_boundary():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    zone = ZoneInfo("Europe/Rome")
    now = int(datetime(2026, 1, 1, 0, 30, tzinfo=zone).timestamp() * 1000)
    earlier = int(datetime(2025, 12, 31, 23, 45, tzinfo=zone).timestamp() * 1000)
    assert fmt.day_group(earlier, now_ms=now, tz="Europe/Rome") == "Yesterday"
    # Repeated autumn hours share one calendar day, independently of elapsed hours.
    first = int(datetime(2026, 10, 25, 2, 30, tzinfo=zone, fold=0).timestamp() * 1000)
    second = int(datetime(2026, 10, 25, 2, 30, tzinfo=zone, fold=1).timestamp() * 1000)
    assert fmt.day_group(first, now_ms=second, tz="Europe/Rome") == "Today"
    assert fmt.row_time(first, tz="Europe/Rome") == fmt.row_time(second, tz="Europe/Rome") == "02:30"


def test_size_rounding_is_half_up():
    assert fmt.size(1048576 // 2) == "1 MB"
    assert fmt.size(1000 * 1048576) == "1.0 GB"
