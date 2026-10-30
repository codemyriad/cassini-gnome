# SPDX-License-Identifier: GPL-3.0-or-later
"""Streaming failure/cancellation must not publish truncated successful exports."""

import os
import threading
from concurrent.futures import CancelledError, ThreadPoolExecutor
from dataclasses import replace

import pytest
from test_export import ASSETS, gst

from cassini_viewer import exporting, portable
from cassini_viewer.exporting import export_meeting
from cassini_viewer.library import LibraryScanner
from cassini_viewer.meeting_store import atomic_json, update_metadata
from cassini_viewer.open_meeting import load_open_meeting
from cassini_viewer.received import import_received
from cassini_viewer.recording_session import RecordingSession
from cassini_viewer.transcription import REFERENCE


def test_streaming_read_error_while_pull_waits_is_not_successful_eos(tmp_path, monkeypatch):
    gst()
    from gi.repository import GObject

    from cassini_viewer.folder_player import PCMSource

    session = RecordingSession(tmp_path)
    session.add_track("mic", "Me", "microphone", 48000, 1)
    session.append("mic", bytes(19200), 0)
    session.finish()
    meeting = LibraryScanner(tmp_path).scan().meetings[0]
    pulling = threading.Event()
    emit = GObject.Object.emit

    def pull(self, name, *args):
        if name == "try-pull-sample":
            pulling.set()
        return emit(self, name, *args)

    def fail(self):
        assert pulling.wait(3), "The test must fail after the pre-pull bus check"
        raise OSError("Synthetic read failure during appsink pull")

    monkeypatch.setattr(GObject.Object, "emit", pull)
    monkeypatch.setattr(PCMSource, "_open", fail)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(export_meeting, meeting, tmp_path / "Exports")
        with pytest.raises(ValueError, match="Synthetic read failure"):
            future.result(timeout=5)
    assert not list((tmp_path / "Exports").iterdir())


@pytest.mark.parametrize("kind", ["symlink", "fifo"])
def test_received_export_rejects_nonregular_audio_before_header_read(tmp_path, kind):
    original = load_open_meeting(ASSETS / "plain-title.opus")
    path = tmp_path / "unsafe.opus"
    if kind == "symlink":
        path.symlink_to(original.audio_file)
    else:
        os.mkfifo(path)
    with pytest.raises((OSError, ValueError)):
        export_meeting(replace(original, audio_file=path), tmp_path / "Exports")
    assert not list((tmp_path / "Exports").iterdir())


@pytest.mark.parametrize("phase", ["digest", "verification"])
def test_export_reader_cancellation_uses_the_dialog_cancellation_type(tmp_path, monkeypatch, phase):
    cancel = threading.Event()
    original = load_open_meeting(ASSETS / "plain-title.opus")

    def interrupted(*args, **kwargs):
        cancel.set()
        raise InterruptedError("cancelled")

    monkeypatch.setattr(portable, "compute_audio_digest" if phase == "digest" else "verify_audio", interrupted)
    with pytest.raises(CancelledError):
        export_meeting(original, tmp_path / "Exports", cancel=cancel)
    assert not list((tmp_path / "Exports").iterdir())


def test_real_io_interruption_is_not_disguised_as_user_cancellation(tmp_path, monkeypatch):
    original = load_open_meeting(ASSETS / "plain-title.opus")

    def interrupted(*args, **kwargs):
        raise InterruptedError("Synthetic IO error")

    monkeypatch.setattr(exporting.portable, "compute_audio_digest", interrupted)
    with pytest.raises(InterruptedError, match="Synthetic IO error"):
        export_meeting(original, tmp_path / "Exports", cancel=threading.Event())
    assert not list((tmp_path / "Exports").iterdir())


def test_silent_received_sidecar_remains_done_after_write_down(tmp_path):
    original = load_open_meeting(ASSETS / "plain-title.opus")
    exported = export_meeting(original, tmp_path / "Exports")
    received = import_received(exported, tmp_path / "Library")
    assert received.transcript == "none"
    atomic_json(received.directory / "transcript.json", dict(format=portable.WORDS_FORMAT, language="und", items=[]))
    update_metadata(received.directory, transcript=dict(status="done", reference=REFERENCE))
    meeting = LibraryScanner(received.directory.parent).scan().meetings[0]
    assert meeting.transcript == "done" and meeting.no_speech and meeting.can_write_down and not meeting.words


def test_empty_embedded_speaker_id_is_damaged_before_import_or_export(tmp_path, monkeypatch):
    from cassini_viewer import portable_writer

    original = portable_writer.chunk_set

    def empty_speaker(prefix, body, mime):
        if prefix == "CASSINI_TX_WORDS_PAYLOAD_":
            body = dict(body, wordCount=1, items=[dict(speaker="", startMs=0, endMs=2, text="Hello")])
        return original(prefix, body, mime)

    monkeypatch.setattr(portable_writer, "chunk_set", empty_speaker)
    model = load_open_meeting(ASSETS / "plain-title.opus")
    path = tmp_path / "empty-speaker.opus"
    with model.audio_file.open("rb") as audio, path.open("wb") as output:
        tags, _ = portable_writer.descriptors(model, portable.compute_audio_digest(audio))
        portable_writer.rewrite_tags(audio, output, tags)
    embedded = portable.read_meeting(str(path))
    with pytest.raises(ValueError, match="speaker"):
        portable.load_words(embedded, embedded.default_transcript_id)
    assert load_open_meeting(path).transcript == "damaged"
    imported = import_received(path, tmp_path / "Library")
    assert imported.transcript == "damaged" and not imported.words and not imported.speakers


def test_flatpak_rejects_writable_tmpfs_home_before_creating_an_export(tmp_path, monkeypatch):
    from cassini_viewer import library_location

    original = load_open_meeting(ASSETS / "plain-title.opus", tmp_path / "Library")
    monkeypatch.setattr("cassini_viewer.companion.in_flatpak", lambda: True)
    monkeypatch.setattr(library_location, "_sandbox_mounts", lambda: (tmp_path / "host-grant",))
    destination = tmp_path / "private-home/Documents"
    with pytest.raises(PermissionError):
        export_meeting(original, destination, "markdown")
    assert not destination.exists() and not list(tmp_path.rglob(".cassini-*"))


def test_flatpak_selected_folder_grant_exports_both_formats_without_a_home_grant(tmp_path, monkeypatch):
    from cassini_viewer import library_location

    original = load_open_meeting(ASSETS / "plain-title.opus", tmp_path / "Library")
    destination = tmp_path / "host-grant"
    destination.mkdir()
    monkeypatch.setattr("cassini_viewer.companion.in_flatpak", lambda: True)
    monkeypatch.setattr(library_location, "_sandbox_mounts", lambda: (destination,))
    markdown = export_meeting(original, destination, "markdown")
    opus = export_meeting(original, destination)
    assert markdown.parent == opus.parent == destination
    assert markdown.read_text().startswith("# ")
    assert portable.verify_audio(str(opus), portable.read_meeting(str(opus))).state == portable.State.OK
    assert not list(destination.glob(".cassini-*"))
