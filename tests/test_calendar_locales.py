# SPDX-License-Identifier: GPL-3.0-or-later
"""D50.3: real CLDR language patterns with independently chosen numeric style."""

import hashlib
import json
import locale
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from cassini_talk import calendar_formats, i18n, strings
from cassini_talk import ux_formats as fmt
from cassini_talk.capture_details import _counter

ROOT = Path(__file__).parents[1]
SUNDAY = int(datetime(2026, 11, 1, 9, tzinfo=ZoneInfo("Europe/Rome")).timestamp() * 1000)
FULL = {
    "en": "Sunday, November 1, 2026",
    "it": "domenica 1 novembre 2026",
    "fr": "dimanche 1 novembre 2026",
    "es": "domingo, 1 de noviembre de 2026",
    "pt-BR": "domingo, 1 de novembro de 2026",
    "pt-PT": "domingo, 1 de novembro de 2026",
}


def preferences(monkeypatch, *, us):
    conventions = dict(
        locale.localeconv(), decimal_point="." if us else ",", thousands_sep="," if us else "\u202f", grouping=[3, 0]
    )
    monkeypatch.setattr(locale, "localeconv", lambda: conventions.copy())
    monkeypatch.setattr(locale, "nl_langinfo", lambda _field: "%r" if us else "%H:%M:%S")
    monkeypatch.setattr(locale, "getlocale", lambda _category: ("en_US" if us else "fr_FR", "UTF-8"))


@pytest.mark.parametrize("language", i18n.LANGUAGES)
@pytest.mark.parametrize("us", [False, True], ids=["french-numbers", "us-numbers"])
def test_full_date_words_and_pattern_are_ui_language_clock_is_format_locale(monkeypatch, language, us):
    before = locale.setlocale(locale.LC_ALL)
    preferences(monkeypatch, us=us)
    with i18n.using_language(language):
        assert fmt.full_date_time(SUNDAY, tz="Europe/Rome") == FULL[language] + (", 9:00 AM" if us else ", 09:00")
    assert locale.setlocale(locale.LC_ALL) == before


@pytest.mark.parametrize("language", i18n.LANGUAGES)
def test_nonrelative_library_and_search_dates_do_not_borrow_lc_time_words(monkeypatch, language):
    preferences(monkeypatch, us=True)
    later = SUNDAY + 3 * 86400000
    with i18n.using_language(language):
        grouped = fmt.day_group(SUNDAY, now_ms=later, tz="Europe/Rome")
        searched = fmt.search_date(SUNDAY, now_ms=later, tz="Europe/Rome")
        assert calendar_formats.CALENDARS[language]["weekdays"][-1] in grouped
        assert calendar_formats.CALENDARS[language]["months"][10] in grouped
        assert calendar_formats.CALENDARS[language]["months"][10] in searched
        assert fmt.day_group(SUNDAY, now_ms=SUNDAY, tz="Europe/Rome") == strings.text("format.today")


@pytest.mark.parametrize("language", ["en", "fr", "pt-BR", "pt-PT"])
@pytest.mark.parametrize("us", [False, True])
def test_numeric_values_and_rates_follow_numeric_locale_independent_of_ui(monkeypatch, language, us):
    preferences(monkeypatch, us=us)
    grouped = "1,234,567" if us else "1\u202f234\u202f567"
    decimal = "1.25" if us else "1,25"
    with i18n.using_language(language):
        assert i18n.number(1234567) == grouped
        assert i18n.number(Decimal("1.25")) == decimal
        assert strings.text("format.playbackRate", rate=Decimal("1.25")) == decimal + "×"
        assert grouped in strings.plural("format.searchCount.places", 1234567)
        assert fmt.size(1280 * 1024**2).startswith("1.3" if us else "1,3")


def test_counter_uses_format_locale_for_frames_and_fractional_seconds(monkeypatch):
    preferences(monkeypatch, us=False)
    with i18n.using_language("en"):
        assert _counter([dict(frames=4000, sampleRate=8000)], "frames") == "4\u202f000 frames · 0,5 s"
    preferences(monkeypatch, us=True)
    with i18n.using_language("fr"):
        assert _counter([dict(frames=4000, sampleRate=8000)], "frames") == "4,000 trames · 0.5 s"


def test_concurrent_document_languages_do_not_swap_any_process_locale(monkeypatch):
    preferences(monkeypatch, us=True)
    before = locale.setlocale(locale.LC_ALL)
    original = locale.setlocale

    def no_swap(category, value=None):
        assert value is None, "Formatting must never set a process-wide locale"
        return original(category)

    monkeypatch.setattr(locale, "setlocale", no_swap)
    barrier = threading.Barrier(2)

    def render(language):
        with i18n.using_language(language):
            barrier.wait(timeout=2)
            return {fmt.full_date_time(SUNDAY, tz="Europe/Rome") for _ in range(50)}

    with ThreadPoolExecutor(2) as pool:
        french, portuguese = pool.map(render, ("fr", "pt-PT"))
    assert french == {"dimanche 1 novembre 2026, 9:00 AM"}
    assert portuguese == {"domingo, 1 de novembro de 2026, 9:00 AM"}
    assert original(locale.LC_ALL) == before


def test_vendored_cldr_bytes_and_license_are_pinned():
    source = json.loads((ROOT / "tests/i18n/CLDR-SOURCES.json").read_text())
    for name, expected in source["vendoredSha256"].items():
        assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == expected
    assert set(calendar_formats.CALENDARS) == set(i18n.LANGUAGES)
    assert "UNICODE LICENSE V3" in (ROOT / "src/cassini_talk/CLDR-LICENSE.txt").read_text()


def test_ui_words_decode_as_utf8_even_with_an_ascii_formatting_environment():
    code = r"""
from datetime import date
from cassini_talk.calendar_formats import full_date
from cassini_talk.strings import text
assert '\u00e9' in full_date(date(2026, 12, 1))
assert '\u00e9' in text('firstRun.body')
print('ok')
"""
    result = subprocess.run(
        [sys.executable, "-X", "utf8=0", "-c", code],
        env=dict(os.environ, LANGUAGE="fr", LC_ALL="C", PYTHONCOERCECLOCALE="0", PYTHONPATH=str(ROOT / "src")),
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    assert result.stdout.strip() == "ok"
