# SPDX-License-Identifier: GPL-3.0-or-later
# ruff: noqa: E402 - runtime-dependent fixtures follow explicit dependency checks.
"""S-12 app scanner: one row across capture/publish/recovery, without writes."""

import hashlib
import shutil

import pytest

pytest.importorskip("numpy")
gi = pytest.importorskip("gi")
try:
    gi.require_version("Gst", "1.0")
except ValueError:
    pytest.skip("Real publication tests require GStreamer", allow_module_level=True)

from library_v2_support import simple_session
from test_library import capture

from cassini_viewer.library import LibraryScanner
from cassini_viewer.library_v2 import finalize, journal


def fingerprints(root):
    return {
        str(path.relative_to(root)): (
            path.stat().st_ino,
            path.stat().st_mtime_ns,
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink()
    }


def test_hidden_capture_becomes_one_published_row_and_scan_never_repairs(tmp_path):
    sid, directory = simple_session(tmp_path, 0.1)
    scanner = LibraryScanner(tmp_path)
    active = scanner.scan(capture_activity={sid: "recording"})
    assert len(active.meetings) == 1 and active.meetings[0].lifecycle == "recording"
    assert active.meetings[0].directory == directory
    assert active.meetings[0].published_file is None
    result = finalize.finalize(tmp_path, sid, zone="Europe/Rome")
    before = fingerprints(tmp_path)
    saving = scanner.scan(capture_activity={sid: "saving"})
    assert len(saving.meetings) == 1
    meeting = saving.meetings[0]
    assert meeting.id == sid and meeting.directory == directory
    assert meeting.lifecycle == "saving"
    assert meeting.published_file == tmp_path / result.name
    assert meeting.audio_file == meeting.published_file
    assert meeting.has_audio and meeting.metadata["published"] is True
    assert scanner.scan().meetings[0].lifecycle == "saved"
    assert fingerprints(tmp_path) == before


def test_root_file_is_complete_without_the_hidden_session(tmp_path):
    sid, directory = simple_session(tmp_path, 0.1)
    result = finalize.finalize(tmp_path, sid, zone="Europe/Rome")
    shutil.rmtree(directory)
    (meeting,) = LibraryScanner(tmp_path).scan().meetings
    assert meeting.id == sid and meeting.origin == "own"
    assert meeting.audio_file == tmp_path / result.name
    assert meeting.has_audio and not meeting.tracks and meeting.metadata_editable
    assert meeting.transcript == "none"


def test_missing_published_file_is_not_an_interrupted_recording(tmp_path):
    sid, _directory = simple_session(tmp_path, 0.1)
    result = finalize.finalize(tmp_path, sid, zone="Europe/Rome")
    (tmp_path / result.name).unlink()
    (meeting,) = LibraryScanner(tmp_path).scan().meetings
    assert meeting.id == sid and meeting.published_missing
    assert meeting.interrupted_at_ms is None and not meeting.can_write_down
    assert any(d.code == "published-file-missing" for d in meeting.diagnostics)


def test_pending_delete_suppresses_file_and_session_before_any_trash_step(tmp_path):
    sid, _directory = simple_session(tmp_path, 0.1)
    result = finalize.finalize(tmp_path, sid, zone="Europe/Rome")
    layout = journal.Layout(tmp_path)
    entry = journal.new_entry(
        "delete",
        origin="Recording",
        session_id=sid,
        phase="intent",
        session=layout.relative(layout.session("Recording", sid)),
        oldPath=result.name,
        newPath=None,
    )
    journal.write(layout, entry)
    before = fingerprints(tmp_path)
    assert not LibraryScanner(tmp_path).scan().meetings
    assert fingerprints(tmp_path) == before


def test_legacy_folder_and_new_published_file_both_remain_readable(tmp_path):
    old = capture(tmp_path)
    old.finish()
    sid, _directory = simple_session(tmp_path, 0.1)
    finalize.finalize(tmp_path, sid, zone="Europe/Rome")
    rows = LibraryScanner(tmp_path).scan().meetings
    assert {m.id for m in rows} == {old.manifest["id"], sid}


def test_stage_partial_dotfile_symlink_and_directory_are_not_meetings(tmp_path):
    for name in (".private.opus", "unfinished.opus.stage", "unfinished.partial"):
        (tmp_path / name).write_bytes(b"bad")
    (tmp_path / "directory.opus").mkdir()
    (tmp_path / "linked.opus").symlink_to(tmp_path / ".private.opus")
    assert not LibraryScanner(tmp_path).scan().meetings
    (tmp_path / "broken.opus").write_bytes(b"bad")
    (meeting,) = LibraryScanner(tmp_path).scan().meetings
    assert meeting.transcript == "damaged" and not meeting.has_audio


def test_published_projection_is_reused_when_another_session_changes(tmp_path):
    sid, _directory = simple_session(tmp_path, 0.1)
    finalize.finalize(tmp_path, sid, zone="Europe/Rome")
    scanner = LibraryScanner(tmp_path)
    (first,) = scanner.scan().meetings
    sid2, _ = simple_session(tmp_path, 0.1, title="Another")
    rows = scanner.scan().meetings
    assert next(m for m in rows if m.id == sid) is first
    assert {m.id for m in rows} == {sid, sid2}


def test_no_x_root_file_is_read_play_only_until_step_five(tmp_path):
    from cassini_viewer import portable
    from cassini_viewer.library_v2 import edit, xschema
    from cassini_viewer.portable_writer import MANIFEST_MIME, chunk_set

    sid, directory = simple_session(tmp_path, 0.1)
    result = finalize.finalize(tmp_path, sid)
    path = tmp_path / result.name
    manifest = portable.read_meeting(str(path)).manifest
    manifest["x"].pop(xschema.DOMAIN)
    tags, _ = chunk_set(edit.MANIFEST_PREFIX, manifest, MANIFEST_MIME)
    with path.open("rb") as stream:
        vendor, fields = edit.comment_fields(stream)
    fields = edit.edited_fields(fields, tags, chunk_prefixes=(edit.MANIFEST_PREFIX,))
    stage = path.with_suffix(".test-stage")
    edit.rewrite(path, stage, fields, vendor)
    edit.verify(stage)
    stage.replace(path)
    shutil.rmtree(directory)
    before = fingerprints(tmp_path)
    (meeting,) = LibraryScanner(tmp_path).scan().meetings
    assert meeting.has_audio and meeting.audio_file == path
    assert not meeting.metadata_editable and not meeting.can_write_down
    assert fingerprints(tmp_path) == before
