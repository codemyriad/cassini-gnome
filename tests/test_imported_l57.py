# SPDX-License-Identifier: GPL-3.0-or-later
"""WP7 / L-57: desktop Imported wording, other-device attribution and fresh-import keys."""

import json
import zipfile
from pathlib import Path

import pytest

from cassini_talk import batch_import
from cassini_talk import ux_formats as fmt
from cassini_talk.i18n import LANGUAGES, using_language
from cassini_talk.library import LibraryScanner
from cassini_talk.strings import ENTRIES, text

ROOT = Path(__file__).resolve().parents[1]
FORMATS = json.loads((ROOT / "tests/ux/format-vectors.json").read_bytes())
CONTEXT = dict(now_ms=FORMATS["nowMs"], tz=FORMATS["tz"])
FIXTURES = Path(__file__).parent / "conformance/opus"
ONE = FIXTURES / "001-minimal-v1.opus"
TWO = FIXTURES / "002-plain-audio.opus"
AT, DURATION = 1792760400000, 2040000


def test_catalog_renames_received_to_imported_without_changing_the_model_identifier():
    assert text("states.badge.received") == "Imported"
    assert "received.importedToast" in ENTRIES and "received.importedToast.noFolder" in ENTRIES


@pytest.mark.parametrize("lang", LANGUAGES)
def test_header_subtitle_matches_both_catalog_templates_in_every_language(lang):
    with using_language(lang):
        day = fmt.day_group(AT, **CONTEXT)
        time = fmt.row_time(AT, tz=CONTEXT["tz"])
        duration = fmt.meeting_duration(DURATION)
        for key, elsewhere in (
            ("format.previewSubtitle.received", False),
            ("format.previewSubtitle.importedOtherDevice", True),
        ):
            expected = text(key, day=day, time=time, duration=duration)
            actual = fmt.preview_subtitle(AT, DURATION, received=True, transcribed_elsewhere=elsewhere, **CONTEXT)
            assert actual == expected
        # The trailing words equal the badge, also when the date and length are unknown.
        unknown = fmt.preview_subtitle(None, None, received=True, **CONTEXT)
        assert unknown.endswith(" · " + text("states.badge.received"))
        assert "\x00" not in fmt.preview_subtitle(None, None, received=True, transcribed_elsewhere=True, **CONTEXT)


def test_own_meetings_never_get_the_imported_suffix():
    plain = fmt.preview_subtitle(AT, DURATION, **CONTEXT)
    assert plain == fmt.preview_subtitle(AT, DURATION, transcribed_elsewhere=True, **CONTEXT)
    assert "Imported" not in plain


def test_only_a_validated_embedded_transcript_is_attributed_to_another_device(tmp_path):
    root = tmp_path / "Library"
    result = batch_import.import_files([ONE, TWO], root)
    assert result.imported == 2
    meetings = {m.metadata["received"]["originalName"]: m for m in LibraryScanner(root).scan().meetings}
    embedded, plain = meetings[ONE.name], meetings[TWO.name]
    assert embedded.transcript == "done" and embedded.legacy_transcript_file is not None
    assert embedded.transcribed_elsewhere
    assert plain.origin == "received" and not plain.transcribed_elsewhere


def test_fresh_import_keys_list_new_copies_only_never_duplicates(tmp_path):
    root = tmp_path / "Library"
    first = batch_import.import_files([ONE], root)
    assert first.added_keys == [first.last_key]
    archive = tmp_path / "Bundle.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("Notes/again.opus", ONE.read_bytes())
        bundle.writestr("Notes/plain.opus", TWO.read_bytes())
    second = batch_import.import_files([ONE, archive], root)
    assert (second.imported, second.duplicates) == (1, 2)
    keys = {m.key for m in LibraryScanner(root).scan().meetings}
    assert len(second.added_keys) == 1 and second.added_keys[0] in keys
    assert first.last_key not in second.added_keys
