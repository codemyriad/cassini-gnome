# SPDX-License-Identifier: GPL-3.0-or-later
"""Run the coordinator's golden files without rewriting or normalising their bytes."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from cassini_talk import ux_formats as fmt
from cassini_talk.i18n import LANGUAGES, using_language
from cassini_talk.meeting_state import MeetingError, MeetingState, import_entry_badge

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
    assert STATES["version"] == 3
    state = MeetingState.from_fields(case["fields"])
    badge = state.badge(live_ms=case["fields"].get("liveMs", 0))
    assert (badge.as_dict() if badge else None) == case["badge"]
    if "canWriteDown" in case:
        assert state.can_write_down is case["canWriteDown"]


@pytest.mark.parametrize("case", STATES["entryVectors"], ids=lambda c: c["id"])
def test_entry_state_vector(case):
    assert case["scope"] == "import-entry"
    badge = import_entry_badge(case["fields"])
    assert (badge.as_dict() if badge else None) == case["badge"]
    assert import_entry_badge({}) is None
    assert import_entry_badge({"importing": False}) is None


@pytest.mark.parametrize("lang", LANGUAGES)
def test_badge_labels_and_interrupted_position_follow_catalog_in_all_languages(lang):
    catalog = {e["id"]: e for e in json.loads((ROOT / "src/cassini_talk/strings.json").read_bytes())["entries"]}

    def expected_entry(entry_id, **kwargs):
        entry = catalog[entry_id]
        template = entry["en"] if lang == "en" else entry["l10n"][lang]["text"]
        return template.format(**kwargs)

    def expected_plural_one_or_other(entry_id, n):
        entry = catalog[entry_id]
        forms = entry["plural"] if lang == "en" else entry["l10n"][lang]["plural"]
        template = forms["one"] if n == 1 else forms["other"]
        return template.format(n=n)

    with using_language(lang):
        assert MeetingState(lifecycle="recording").badge(live_ms=768000).as_dict() == {
            "kind": "recording",
            "text": expected_entry("states.badge.recording", liveBadge="12:48"),
            "tint": "red",
            "glyph": "dot",
        }
        assert import_entry_badge({"importing": True}).as_dict() == {
            "kind": "importing",
            "text": expected_entry("states.badge.importing"),
            "tint": "neutral",
            "glyph": "spinner",
        }
        assert MeetingState(lifecycle="saving").badge().as_dict() == {
            "kind": "saving",
            "text": expected_entry("states.badge.saving"),
            "tint": "neutral",
            "glyph": "spinner",
        }
        assert MeetingState(transcript="running", eta_minutes=4).badge().as_dict() == {
            "kind": "transcribing",
            "text": expected_plural_one_or_other("states.badge.transcribing", 4),
            "tint": "neutral",
            "glyph": "spinner",
        }
        assert MeetingState(transcript="running", eta_minutes=0).badge().text == expected_entry(
            "states.badge.transcribing.underMinute"
        )
        assert MeetingState(transcript="queued", eta_minutes=None).badge().text == expected_entry(
            "states.badge.transcribing.noEta"
        )
        assert MeetingState(
            transcript="failed", error=MeetingError("transcribe-failed", "retry")
        ).badge().as_dict() == {
            "kind": "needsAttention",
            "text": expected_entry("states.badge.needsAttention"),
            "tint": "amber",
            "glyph": "alert",
        }
        interrupted = MeetingState(interrupted_at_ms=2472000)
        assert interrupted.badge().as_dict() == {
            "kind": "interrupted",
            "text": expected_entry("states.badge.interrupted"),
            "tint": "amber",
            "glyph": "alert",
        }
        assert interrupted.interrupted_text() == expected_entry("meeting.interruptedAt", position="00:41:12")
        assert MeetingState().interrupted_text() is None
        assert MeetingState(origin="received").badge().as_dict() == {
            "kind": "received",
            "text": expected_entry("states.badge.received"),
            "tint": "outline",
            "glyph": "incoming",
        }


def test_golden_bytes():
    subprocess.run([sys.executable, str(ROOT / "build-aux/check-ux-vectors.py")], check=True)


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
    assert fmt.size(1048576 // 2) == "512 KB"
    assert fmt.size(1000 * 1048576) == "1.0 GB"


@pytest.mark.parametrize(
    "size,expected",
    [
        (0, "0 KB"),
        (1, "1 KB"),
        (512, "1 KB"),
        (1024, "1 KB"),
        (1536, "2 KB"),
        (1023487, "999 KB"),
        (1023488, "1 MB"),
        (1048575, "1 MB"),
        (1048576, "1 MB"),
    ],
)
def test_sizes_below_one_mib_use_shared_kb_copy(size, expected):
    assert fmt.size(size) == expected
    assert fmt.size(size, approx=True) == "about " + expected


def test_local_time_style_uses_twelve_hours_without_changing_durations(monkeypatch):
    import locale

    monkeypatch.setattr(locale, "nl_langinfo", lambda _: "%r")
    at = FORMATS["nowMs"]
    assert fmt.row_time(at, tz=FORMATS["tz"]) == "2:18 PM"
    assert "2:18 PM" in fmt.preview_subtitle(at, 90000, now_ms=at, tz=FORMATS["tz"])
    assert fmt.position(14 * 60 * 60 * 1000) == "14:00:00"
    monkeypatch.setattr(locale, "nl_langinfo", lambda _: "%H:%M:%S")
    assert fmt.row_time(at, tz=FORMATS["tz"]) == "14:18"


@pytest.mark.parametrize("locale_name,expected", [("en_GB.UTF-8", "14:18"), ("en_US.UTF-8", "2:18 PM")])
def test_locale_clock_style_in_a_real_locale_process(locale_name, expected):
    code = """import locale,sys
try: locale.setlocale(locale.LC_TIME,sys.argv[1])
except locale.Error: sys.exit(77)
from cassini_talk.ux_formats import row_time
print(row_time(1793107080000,tz='Europe/Rome'))
"""
    import os

    result = subprocess.run(
        [sys.executable, "-c", code, locale_name],
        env=dict(os.environ, PYTHONPATH=str(ROOT / "src")),
        capture_output=True,
        text=True,
    )
    if result.returncode == 77:
        pytest.skip("Locale is generated by the UX workflow; unavailable in this source image")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected


def test_preview_subtitle_imported_and_transcribed_on_other_device():
    at = 1792760400000
    assert (
        fmt.preview_subtitle(at, 2040000, now_ms=FORMATS["nowMs"], tz=FORMATS["tz"], received=True)
        == "Friday 23 October, 15:00 · 34 min · Imported"
    )
    assert (
        fmt.preview_subtitle(
            at,
            2040000,
            now_ms=FORMATS["nowMs"],
            tz=FORMATS["tz"],
            received=True,
            transcribed_other_device=True,
        )
        == "Friday 23 October, 15:00 · 34 min · Imported · Transcribed on another device"
    )
    assert (
        fmt.preview_subtitle(
            at,
            None,
            now_ms=FORMATS["nowMs"],
            tz=FORMATS["tz"],
            received=True,
            transcribed_other_device=True,
        )
        == "Friday 23 October, 15:00 · Imported · Transcribed on another device"
    )
