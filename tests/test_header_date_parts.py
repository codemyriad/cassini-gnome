# SPDX-License-Identifier: GPL-3.0-or-later
"""D-1177: the meeting header's date is its own button; the rest of the line stays plain text."""

import json
from pathlib import Path

import pytest

from cassini_talk import ux_formats as fmt
from cassini_talk.i18n import LANGUAGES, using_language

ROOT = Path(__file__).resolve().parents[1]
FORMATS = json.loads((ROOT / "tests/ux/format-vectors.json").read_bytes())
CONTEXT = dict(now_ms=FORMATS["nowMs"], tz=FORMATS["tz"])
AT, DURATION = 1792760400000, 2040000


@pytest.mark.parametrize("lang", LANGUAGES)
@pytest.mark.parametrize("at", [AT, None])
@pytest.mark.parametrize("duration", [DURATION, None])
@pytest.mark.parametrize("received,elsewhere", [(False, False), (True, False), (True, True)])
def test_date_and_rest_rejoin_into_the_subtitle(lang, at, duration, received, elsewhere):
    kwargs = dict(received=received, transcribed_elsewhere=elsewhere, **CONTEXT)
    with using_language(lang):
        date, rest = fmt.preview_subtitle_parts(at, duration, **kwargs)
        assert date + rest == fmt.preview_subtitle(at, duration, **kwargs)
        expected = fmt.day_group(at, **CONTEXT) + (f", {fmt.row_time(at, tz=CONTEXT['tz'])}" if at else "")
        assert date == expected
        # The length and "Imported" never leak into the clickable date.
        if duration is not None:
            assert fmt.meeting_duration(duration) in rest
