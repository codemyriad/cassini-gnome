# SPDX-License-Identifier: GPL-3.0-or-later
import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

from cassini_viewer.library import LibraryScanner, library_root, scan_library
from cassini_viewer.recording_session import RecordingSession

ROOT = Path(__file__).resolve().parents[1]


def write_metadata(directory, **changes):
    value = dict(
        version=1,
        revision=0,
        id=directory.name.split("-", 1)[1],
        title=None,
        titleSource="meeting-window",
        notes=[],
        speakers=[],
        transcript=dict(status="none", reference=None),
        etaMinutes=None,
        error=None,
        quoteOverride=None,
        received=None,
    )
    value.update(changes)
    (directory / "meeting.json").write_text(json.dumps(value))
    return value


def capture(root, *, completed=True):
    session = RecordingSession(root, "Capture title")
    session.add_track("mic", "Not a person", "microphone", 48000, 1)
    session.add_track("others", "Raw receiver is not a speaker", "remote", 48000, 1)
    session.append("mic", b"\x01\x00" * 4800, 250)
    session.append("others", b"\x01\x00" * 9600, 500)
    session.checkpoint()
    if completed:
        session.finish()
    return session


def fingerprint(directory):
    return {
        p.name: (p.read_bytes(), p.stat().st_mode, p.stat().st_mtime_ns) for p in directory.iterdir() if p.is_file()
    }


def test_capture_uses_actual_pcm_timeline_and_keeps_capture_bytes_unchanged(tmp_path):
    session = capture(tmp_path)
    # Header/manifest checkpoints may lag physical complete PCM.
    audio = session.directory / session.manifest["tracks"][1]["file"]
    with audio.open("ab") as stream:
        stream.write(bytes(960))
    write_metadata(session.directory, title="App title", titleSource="user", futureMember={"keep": "exactly"})
    before = fingerprint(session.directory)
    meeting = scan_library(tmp_path).meetings[0]
    assert meeting.id == session.manifest["id"] and meeting.title == "App title"
    assert meeting.lifecycle == "saved" and meeting.origin == "own"
    assert meeting.duration_ms == 710 and meeting.interrupted_at_ms is None
    assert meeting.speaker_names == (), "source labels must never become participant names"
    assert meeting.metadata["futureMember"] == {"keep": "exactly"}
    assert fingerprint(session.directory) == before


def test_activity_is_injected_not_inferred_from_recording_manifest_or_mtime(tmp_path):
    session = capture(tmp_path, completed=False)
    try:
        identifier = session.manifest["id"]
        abandoned = scan_library(tmp_path).meetings[0]
        assert abandoned.lifecycle == "saved" and abandoned.interrupted_at_ms == 700
        live = scan_library(tmp_path, capture_activity={identifier: "recording"}).meetings[0]
        assert live.lifecycle == "recording" and live.interrupted_at_ms is None
        saving = scan_library(tmp_path, capture_activity={identifier: "saving"}).meetings[0]
        assert saving.lifecycle == "saving" and saving.interrupted_at_ms is None
    finally:
        session.finish()


def test_corrupt_metadata_salvage_uses_d3_display_zero_without_inventing_alignment(tmp_path):
    session = capture(tmp_path)
    (session.directory / "session.json").write_bytes(b"{bad")
    before = fingerprint(session.directory)
    meeting = scan_library(tmp_path).meetings[0]
    assert meeting.duration_ms == meeting.interrupted_at_ms == 200
    assert all(t.start_offset_ms is None for t in meeting.tracks)
    assert any(d.code == "start-offset-unknown-display-zero" for d in meeting.diagnostics)
    assert fingerprint(session.directory) == before


def test_named_native_tracks_are_read_from_manifest(tmp_path):
    session = capture(tmp_path)
    manifest = session.manifest
    for value, name in zip(manifest["tracks"], ["track-microphone.wav", "track-system.wav"], strict=True):
        (session.directory / value["file"]).rename(session.directory / name)
        value["file"] = name
    (session.directory / "session.json").write_text(json.dumps(manifest))
    assert scan_library(tmp_path).meetings[0].duration_ms == 700


def test_transcript_order_quote_override_progress_and_interruption_are_independent(tmp_path):
    session = capture(tmp_path)
    session.manifest["state"] = "interrupted"
    (session.directory / "session.json").write_text(json.dumps(session.manifest))
    body = dict(
        language="en",
        items=[
            dict(speaker="second", startMs=4000, endMs=5000, text="Later."),
            dict(speaker="first", startMs=1000, endMs=6000, text="The longest early turn."),
            dict(speaker="late", startMs=700000, endMs=800000, text="Outside the first ten minutes."),
        ],
    )
    (session.directory / "transcript.json").write_text(json.dumps(body))
    metadata = write_metadata(
        session.directory,
        transcript=dict(
            status="running", reference=dict(kind="sidecar", file="transcript.json", format="cassini.words.v1")
        ),
        etaMinutes=4,
        speakers=[
            dict(id="second", name=None, provenance="unnamed", colourIndex=1),
            dict(id="first", name="Marta", provenance="user", colourIndex=0),
        ],
    )
    meeting = scan_library(tmp_path).meetings[0]
    assert meeting.speaker_names == ("Marta", "Speaker 2", "Speaker 3")
    assert meeting.quote_line == "“The longest early turn.”"
    assert meeting.interrupted_at_ms == 700 and meeting.transcript == "running"
    assert meeting.badge().kind == "transcribing"
    metadata["transcript"]["status"] = "done"
    metadata["etaMinutes"] = None
    metadata["quoteOverride"] = dict(atMs=4000, text="Last played turn.")
    (session.directory / "meeting.json").write_text(json.dumps(metadata))
    final = scan_library(tmp_path).meetings[0]
    assert final.quote_line == "“Last played turn.”" and final.badge().kind == "interrupted"


def received(root, fixture="001-minimal-v1.opus"):
    import hashlib

    directory = root / f"Received-{uuid4()}"
    directory.mkdir()
    original = directory / "original.opus"
    original.write_bytes((ROOT / "tests/conformance/opus" / fixture).read_bytes())
    write_metadata(
        directory,
        transcript=dict(status="none", reference=None)
        if fixture == "002-plain-audio.opus"
        else dict(status="done", reference=dict(kind="embedded", file=original.name)),
        received=dict(
            originalName=original.name,
            sha256=hashlib.sha256(original.read_bytes()).hexdigest(),
            importedAtMs=1793107080000,
        ),
    )
    return directory


def test_received_reads_original_audio_and_embedded_transcript_without_inventing_provenance(tmp_path):
    directory = received(tmp_path)
    before = fingerprint(directory)
    scanner = LibraryScanner(tmp_path)
    meeting = scanner.scan().meetings[0]
    assert meeting.origin == "received" and meeting.lifecycle == "saved" and meeting.transcript == "done"
    assert meeting.duration_ms == 60 and meeting.quote_line
    assert meeting.speakers and all(s.provenance is None for s in meeting.speakers)
    assert not meeting.can_write_down and meeting.badge().kind == "received"
    assert scanner.scan().meetings == (meeting,)
    assert fingerprint(directory) == before


def test_damaged_received_transcript_does_not_hide_audio_or_raise_action_badge(tmp_path):
    directory = received(tmp_path, "009-bad-payload-sha256.opus")
    meeting = scan_library(tmp_path).meetings[0]
    assert meeting.audio_file.is_file() and meeting.duration_ms == 60
    assert meeting.transcript == "damaged" and meeting.error.id == "transcript-damaged"
    assert meeting.badge().kind == "received" and not meeting.can_write_down
    assert fingerprint(directory)


def test_missing_received_transcript_can_be_written_down_once(tmp_path):
    received(tmp_path, "002-plain-audio.opus")
    meeting = scan_library(tmp_path).meetings[0]
    assert meeting.transcript == "none" and meeting.can_write_down


def test_failed_audio_integrity_keeps_granule_duration_but_withholds_transcript(tmp_path, monkeypatch):
    from cassini_viewer import portable

    directory = received(tmp_path)
    before = fingerprint(directory)

    def incomplete_audio(_stream):
        raise ValueError("incomplete audio packets")

    monkeypatch.setattr(portable, "compute_audio_digest", incomplete_audio)
    meeting = scan_library(tmp_path).meetings[0]
    assert meeting.duration_ms == 60 and meeting.transcript == "damaged"
    assert meeting.quote_line is None and not meeting.speakers and not meeting.can_write_down
    assert any(d.code == "transcript-unreadable" for d in meeting.diagnostics)
    assert fingerprint(directory) == before


def test_missing_transcription_eta_keeps_running_without_inventing_an_estimate(tmp_path):
    session = capture(tmp_path)
    write_metadata(session.directory, transcript=dict(status="running", reference=None))
    meeting = scan_library(tmp_path).meetings[0]
    assert meeting.transcript == "running" and meeting.eta_minutes is None
    assert meeting.badge().text == "Transcribing…" and not meeting.can_write_down


def test_bad_metadata_and_audio_still_produce_a_stable_row(tmp_path):
    directory = tmp_path / f"Recording-{uuid4()}"
    directory.mkdir()
    (directory / "session.json").write_bytes(b"")
    (directory / "meeting.json").write_text("[]")
    first = scan_library(tmp_path).meetings[0]
    second = scan_library(tmp_path).meetings[0]
    assert first == second and first.id == directory.name[10:]
    assert first.lifecycle == "saved" and first.interrupted_at_ms is None
    assert first.duration_ms is None and first.title == "Untitled meeting" and not first.can_write_down
    assert first.started_at_ms is None and first.diagnostics


def test_escaping_references_are_not_followed_and_invalid_folders_are_ignored(tmp_path):
    root = tmp_path / "library"
    root.mkdir()
    session = capture(root)
    outside = tmp_path / "outside.wav"
    audio = session.directory / session.manifest["tracks"][0]["file"]
    outside.write_bytes(audio.read_bytes())
    audio.unlink()
    audio.symlink_to(outside)
    metadata = write_metadata(
        session.directory,
        transcript=dict(
            status="done", reference=dict(kind="sidecar", file="../private.json", format="cassini.words.v1")
        ),
    )
    (tmp_path / "private.json").write_text("{}")
    (root / "Recording-not-a-uuid").mkdir()
    (root / f"Recording-{uuid4()}").symlink_to(tmp_path, target_is_directory=True)
    result = scan_library(root)
    assert len(result.meetings) == 1 and len(result.meetings[0].tracks) == 1
    assert result.meetings[0].transcript == "none", "invalid app metadata is ignored whole"
    assert result.diagnostics[0].code == "unsafe-meeting-directory"
    assert metadata["transcript"]["reference"]["file"] == "../private.json"


def test_default_root_is_literal_and_missing_scan_never_creates_it(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CASSINI_LIBRARY_DIR", raising=False)
    monkeypatch.setenv("XDG_MUSIC_DIR", str(tmp_path / "elsewhere"))
    from cassini_viewer import library_location

    monkeypatch.setattr(library_location, "music_directory", lambda: tmp_path / "Musica")
    assert library_root() == tmp_path / "Musica/Cassini"
    assert not scan_library().meetings and not (tmp_path / "Musica").exists()
    monkeypatch.setenv("CASSINI_LIBRARY_DIR", str(tmp_path / "injected"))
    assert library_root() == tmp_path / "injected"


def test_failed_scan_is_distinct_from_empty(tmp_path, monkeypatch):
    original = os.open

    def blocked(path, *args, **kwargs):
        if Path(path) == tmp_path:
            raise PermissionError("synthetic denied Library")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", blocked)
    result = scan_library(tmp_path)
    assert result.unavailable and result.diagnostics[0].code == "library-unreadable"


def test_pure_core_import_does_not_load_gi():
    subprocess.run(
        [
            sys.executable,
            "-c",
            'import sys;from cassini_viewer.library import LibraryScanner;assert "gi" not in sys.modules',
        ],
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
        check=True,
    )


def test_unknown_metadata_version_is_retained_read_only_not_interpreted(tmp_path):
    session = capture(tmp_path)
    raw = write_metadata(session.directory, version=2, title="Future title", future={"a": 1})
    before = fingerprint(session.directory)
    meeting = scan_library(tmp_path).meetings[0]
    assert meeting.title == "Capture title" and meeting.metadata == raw
    assert any(d.code == "unsupported-version" for d in meeting.diagnostics)
    assert fingerprint(session.directory) == before


def test_gap_history_and_damaged_sidecar_override_done_status(tmp_path):
    session = capture(tmp_path)
    session.manifest["tracks"][0]["gapFrames"] = 480
    (session.directory / "session.json").write_text(json.dumps(session.manifest))
    write_metadata(
        session.directory,
        transcript=dict(
            status="done", reference=dict(kind="sidecar", file="transcript.json", format="cassini.words.v1")
        ),
    )
    (session.directory / "transcript.json").write_text("{invalid")
    meeting = scan_library(tmp_path).meetings[0]
    assert meeting.interrupted_at_ms == 700 and meeting.transcript == "damaged"
    assert meeting.badge().kind == "interrupted", "non-actionable transcript damage must not replace history"


def test_bad_received_reference_does_not_read_outside_library(tmp_path):
    directory = received(tmp_path)
    raw = json.loads((directory / "meeting.json").read_text())
    raw["received"]["originalName"] = "../outside.opus"
    (directory / "meeting.json").write_text(json.dumps(raw))
    assert not scan_library(tmp_path).meetings, "Received publication must pass the whole schema"


def test_none_and_failed_never_publish_stored_body_or_speakers(tmp_path):
    session = capture(tmp_path)
    (session.directory / "transcript.json").write_text(
        json.dumps(dict(language="en", items=[dict(speaker="a", startMs=0, endMs=100, text="Stale words")]))
    )
    scanner = LibraryScanner(tmp_path)
    for status in ("none", "failed"):
        write_metadata(
            session.directory,
            transcript=dict(
                status=status, reference=dict(kind="sidecar", file="transcript.json", format="cassini.words.v1")
            ),
        )
        meeting = scanner.scan().meetings[0]
        assert meeting.transcript == status
        assert not meeting.words and not meeting.speakers and not meeting.quote_line and not meeting.preview_turns


def test_preview_filters_asides_before_limit_and_reuses_grouping_on_metadata_change(tmp_path, monkeypatch):
    from test_transcript import WORDS, w

    from cassini_viewer import library

    session = capture(tmp_path)
    words = WORDS + [w("d", 4000, 4400, "Third visible turn")]
    (session.directory / "transcript.json").write_text(
        json.dumps(
            dict(
                language="en",
                items=[dict(speaker=w.speaker, startMs=w.start_ms, endMs=w.end_ms, text=w.text) for w in words],
            )
        )
    )

    def metadata(title):
        write_metadata(
            session.directory,
            title=title,
            titleSource="user",
            transcript=dict(
                status="done", reference=dict(kind="sidecar", file="transcript.json", format="cassini.words.v1")
            ),
        )

    calls = []
    original = library.build_turns

    def grouped(words):
        calls.append(True)
        return original(words)

    monkeypatch.setattr(library, "build_turns", grouped)
    metadata("Before")
    scanner = LibraryScanner(tmp_path)
    before = scanner.scan().meetings[0]
    assert [t.speaker for t in before.preview_turns] == ["a", "c", "d"]
    metadata("After")
    after = scanner.scan().meetings[0]
    assert after.title == "After" and after.preview_turns is before.preview_turns
    assert len(calls) == 1


def test_missing_root_and_unreadable_folder_evict_all_cached_bodies(tmp_path, monkeypatch):
    import shutil

    session = capture(tmp_path / "library")
    scanner = LibraryScanner(session.directory.parent)
    scanner.scan()
    assert scanner._files.entries and scanner._row_cache
    original = os.open

    def denied(path, *args, **kwargs):
        if path == session.directory.name:
            raise PermissionError("synthetic meeting access failure")
        return original(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(os, "open", denied)
        scanner.scan()
        assert not scanner._row_cache and not scanner._files.entries
    scanner.scan()
    assert scanner._files.entries
    shutil.rmtree(scanner.root)
    assert not scanner.scan().meetings
    assert not scanner._files.entries and not scanner._directories and not scanner._preview_cache


def test_long_preview_bounds_legacy_grouping_and_retains_full_reading_body(tmp_path, monkeypatch):
    from cassini_viewer import library, portable

    scanner = LibraryScanner(tmp_path)
    original = library.build_turns
    observed = []

    def bounded(words):
        observed.append(len(words))
        assert sum(i == 0 or word.speaker != words[i - 1].speaker for i, word in enumerate(words)) <= 32
        return original(words)

    monkeypatch.setattr(library, "build_turns", bounded)
    words = tuple(portable.Word(str(index % 2), index * 1000, index * 1000 + 900, "word") for index in range(20000))
    result = scanner._preview("long", words)
    assert len(result) == 3
    assert sum(len(turn.words) for turn in scanner._preview_cache["long"][2]) == 20000
    assert scanner._preview("long", words) is result and observed == [32]


def test_default_title_is_a_placeholder_but_real_identical_titles_are_not(tmp_path):
    session = RecordingSession(tmp_path, "")
    session.finish()
    write_metadata(session.directory)
    default = scan_library(tmp_path).meetings[0]
    assert default.title == "Untitled meeting" and default.title_is_fallback
    write_metadata(session.directory, title="Untitled meeting", titleSource="user")
    named = scan_library(tmp_path).meetings[0]
    assert named.title == "Untitled meeting" and not named.title_is_fallback


def test_unreadable_meeting_title_is_a_localised_fallback(tmp_path):
    # Regression (review 118 #4): unreadable folders stored English text as a
    # real (editable) title.
    from cassini_viewer.library import _unreadable
    from cassini_viewer.strings import text

    directory = tmp_path / f"Recording-{uuid4()}"
    directory.mkdir()
    for activity in (None, "recording"):
        meeting = _unreadable(directory, directory.name[10:], activity)
        assert meeting.title == text("library.untitled") and meeting.title_is_fallback
