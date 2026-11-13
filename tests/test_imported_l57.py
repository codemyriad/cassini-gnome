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


def test_document_portal_id_is_never_shown_as_the_source_folder(tmp_path, monkeypatch):
    runtime = tmp_path / "run"
    portal = runtime / "doc" / "4a8b9c12"
    portal.mkdir(parents=True)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    (portal / "call.opus").write_bytes(ONE.read_bytes())
    folder = tmp_path / "Downloads"
    folder.mkdir()
    (folder / "plain.opus").write_bytes(TWO.read_bytes())
    root = tmp_path / "Library"
    result = batch_import.import_files([portal / "call.opus", folder / "plain.opus"], root)
    assert result.imported == 2
    received = {
        m.metadata["received"]["originalName"]: m.metadata["received"] for m in LibraryScanner(root).scan().meetings
    }
    assert "originalFolderName" not in received["call.opus"]
    assert received["plain.opus"]["originalFolderName"] == "Downloads"


def _published(tmp_path, reference):
    import dataclasses
    import os

    from cassini_talk.library_v2_index import PublishedIndex, root_audio

    root = tmp_path / "Library"
    root.mkdir()
    path = root / "2026-10-30--18.02-call.opus"
    path.write_bytes(ONE.read_bytes())
    with path.open("rb") as stream:
        audio = root_audio(stream, os.stat(path))
    if reference is not None:
        ours = dict(
            schema=1,
            sessionId="8b7d3c2e-5f1a-4c9e-9a6b-1d2e3f4a5b6c",
            origin="Received",
            namePrefix="2026-10-30--18.02-",
            revision=1,
            meeting=dict(version=1, title=None, transcript=dict(status="done", reference=reference)),
        )
        audio = dataclasses.replace(audio, ours=ours)
    meeting, *_ = PublishedIndex(LibraryScanner(root))._meeting(path, audio, {}, {}, True)
    return meeting


def test_root_cassini_file_with_its_own_transcript_is_attributed_to_another_device(tmp_path):
    meeting = _published(tmp_path, None)
    assert meeting.origin == "received" and meeting.transcript == "done"
    assert meeting.transcribed_elsewhere


@pytest.mark.parametrize(
    ("reference", "elsewhere"),
    [
        (dict(kind="embedded", file="call.opus"), True),
        (dict(kind="sidecar", file="transcript.json", format="cassini.words.v1"), False),
    ],
)
def test_published_received_file_keeps_attribution_only_for_its_embedded_transcript(tmp_path, reference, elsewhere):
    meeting = _published(tmp_path, reference)
    assert meeting.origin == "received" and meeting.transcript == "done"
    assert meeting.transcribed_elsewhere is elsewhere


def test_externally_opened_file_with_a_transcript_is_attributed_to_another_device():
    from cassini_talk.open_meeting import load_open_meeting

    assert load_open_meeting(ONE).transcribed_elsewhere
    assert not load_open_meeting(TWO).transcribed_elsewhere
