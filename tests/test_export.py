# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared export goldens and independent-reader/actual audio round trips."""

import array
import hashlib
import json
import math
import runpy
import sys
import threading
from concurrent.futures import CancelledError, ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from cassini_talk import portable
from cassini_talk.export_audio import limited_pcm
from cassini_talk.export_model import encode_choice, file_stem, markdown
from cassini_talk.exporting import export_meeting
from cassini_talk.library import LibraryScanner
from cassini_talk.open_meeting import load_open_meeting
from cassini_talk.received import import_received
from cassini_talk.recording_session import RecordingSession

HERE = Path(__file__).parent / "export"
ASSETS = Path(__file__).parents[1] / "ux/library-vectors/assets"
MARKDOWN = json.loads((HERE / "markdown-cases.json").read_text())["cases"]
ENCODES = json.loads((HERE / "export-vectors.json").read_text())["vectors"]


def test_export_contract_bytes_are_pinned():
    for name, sha in json.loads((HERE / "UPSTREAM.json").read_text())["sha256"].items():
        assert hashlib.sha256((HERE / name).read_bytes()).hexdigest() == sha


@pytest.mark.parametrize("case", MARKDOWN, ids=lambda c: c["name"])
def test_shared_markdown_golden(case):
    assert markdown(case["input"]).encode() == case["expected"].encode()


@pytest.mark.parametrize("case", ENCODES, ids=lambda c: c["name"])
def test_all_shared_encode_choices(case):
    choice = encode_choice(case["sources"])
    assert {key: choice[key] for key in ("bitrateBps", "cutoffHz")} == {
        key: case[key] for key in ("bitrateBps", "cutoffHz")
    }
    assert choice["policy"] == "match-source" and choice["application"] == "audio"


def test_file_names_cannot_escape_folder_and_fit_unicode_filesystems():
    assert file_stem(" /a\\b\n ") == "-a-b-"
    assert file_stem("  ") == "Untitled meeting"
    assert len(file_stem("x" * 200)) == 120
    assert len(file_stem("🙂" * 120).encode()) <= 230


@pytest.mark.parametrize("length", [1, 100, 240, 241, 2048, 48000])
def test_limiter_preserves_short_and_long_timeline_and_quiet_sound(length):
    quiet = [0.1] * length
    blocks = [quiet[i : i + 73] for i in range(0, length, 73)]
    result = [v for block in limited_pcm(blocks) for v in block]
    assert len(result) == length and all(abs(v - 0.1) < 1e-6 for v in result)
    signal = [1.9 if i % 511 == 0 else 0.2 for i in range(length)]
    result = [v for block in limited_pcm([signal[: length // 2], signal[length // 2 :]]) for v in block]
    assert len(result) == length and max(abs(v) for v in result) <= 0.950001
    assert list(limited_pcm([])) == []


def check_portable(path):
    independent = runpy.run_path(str(HERE / "cassini-read-pure.py"))["read_file"]
    result, size = independent(path)
    assert result["state"] == "unverified" and size == path.stat().st_size
    jsonschema = pytest.importorskip("jsonschema")
    jsonschema.Draft202012Validator(json.loads((HERE / "manifest.schema.json").read_text())).validate(
        result["manifest"]
    )
    value = portable.read_meeting(str(path))
    assert portable.verify_audio(str(path), value).state == portable.State.OK
    return result, value


def test_empty_round_trip_is_writable_preserves_notes_private_data_and_deduplicates(tmp_path, monkeypatch):
    root = tmp_path / "Library"
    monkeypatch.setenv("CASSINI_LIBRARY_DIR", str(root))
    source = ASSETS / "plain-title.opus"
    before = source.read_bytes()
    original = load_open_meeting(source, root)
    notes = (dict(atMs=4, text="Private note", extra={"keep": True}), dict(atMs=1, text="Earlier"))
    model = replace(
        original,
        title="Received example",
        transcript="running",
        words=(portable.Word("x", 0, 2, "Partial"),),
        notes=notes,
        extensions={"example.org": {"opaque": [1, 2]}, "gocassini.com": {"other": "kept"}},
    )
    exported = export_meeting(model, tmp_path / "Documents")
    result, embedded = check_portable(exported)
    assert (
        result["words"] == []
        and "provenance" not in result["manifest"]
        and "readableTranscripts" not in result["manifest"]
    )
    assert result["manifest"]["x"] == {
        "example.org": {"opaque": [1, 2]},
        "gocassini.com": {"other": "kept", "notes": list(notes)},
    }
    direct = load_open_meeting(exported, root)
    assert direct.transcript == "none" and not direct.words and direct.has_audio and direct.notes == notes
    received = import_received(exported, root)
    assert (
        received.origin == "received"
        and received.can_write_down
        and received.transcript == "none"
        and not received.words
    )
    assert received.notes == notes and received.metadata["received"]["originalFolderName"] == "Documents"
    again = import_received(exported, root)
    assert again.key == received.key and len(list(root.glob("Received-*"))) == 1
    rewritten = export_meeting(received, tmp_path / "Documents")
    rewritten_result, rewritten_embedded = check_portable(rewritten)
    assert rewritten_result["manifest"]["x"] == result["manifest"]["x"]
    assert rewritten_embedded.manifest["integrity"] == embedded.manifest["integrity"]
    assert source.read_bytes() == before and not list(root.glob(".received-*"))


def test_concurrent_exports_never_replace_existing_files_and_cancel_leaves_no_partials(tmp_path):
    model = load_open_meeting(ASSETS / "plain-title.opus", tmp_path / "Library")
    directory = tmp_path / "Documents"
    directory.mkdir()
    stem = file_stem(model.title)
    original = directory / (stem + ".md")
    original.write_text("Existing user file")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: export_meeting(model, directory, "markdown"), range(2)))
    assert len(set(results)) == 2 and all(path != original for path in results)
    assert original.read_text() == "Existing user file"
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(CancelledError):
        export_meeting(model, directory, cancel=cancel)
    assert not list(directory.glob(".cassini-*"))


def test_changed_received_copy_is_not_silently_deduplicated(tmp_path, monkeypatch):
    root = tmp_path / "Library"
    monkeypatch.setenv("CASSINI_LIBRARY_DIR", str(root))
    first = import_received(ASSETS / "plain-title.opus", root)
    first.audio_file.write_bytes(b"Changed by another program")
    second = import_received(ASSETS / "plain-title.opus", root)
    assert second.key != first.key and second.has_audio
    assert first.audio_file.read_bytes() == b"Changed by another program"


def gst():
    gi = pytest.importorskip("gi")
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst

    Gst.init(None)
    return Gst


def test_reexport_stereo_keeps_packets_channels_and_integrity(tmp_path):
    Gst = gst()
    audio = tmp_path / "stereo.opus"
    pipeline = Gst.parse_launch(
        "audiotestsrc num-buffers=10 ! audioconvert ! audio/x-raw,rate=48000,channels=2 ! opusenc ! oggmux ! filesink name=output"
    )
    pipeline.get_by_name("output").set_property("location", str(audio))
    pipeline.set_state(Gst.State.PLAYING)
    try:
        message = pipeline.get_bus().timed_pop_filtered(8 * Gst.SECOND, Gst.MessageType.ERROR | Gst.MessageType.EOS)
        assert message and message.type == Gst.MessageType.EOS
    finally:
        pipeline.set_state(Gst.State.NULL)
    before = audio.read_bytes()
    original = load_open_meeting(audio, tmp_path / "Library")
    first = export_meeting(original, tmp_path / "Documents")
    imported = load_open_meeting(first, tmp_path / "Library")
    second = export_meeting(imported, tmp_path / "Documents")
    _, initial = check_portable(first)
    _, final = check_portable(second)
    with audio.open("rb") as stream:
        actual = portable.compute_audio_digest(stream)
    assert initial.channels == final.channels == 2
    assert initial.manifest["integrity"] == final.manifest["integrity"]
    assert final.manifest["integrity"]["opusAudioSha256"] == actual["sha256"]
    assert audio.read_bytes() == before


def test_pcm_export_uses_offsets_stereo_average_and_shared_fullband_choice(tmp_path):
    Gst = gst()
    session = RecordingSession(tmp_path, "PCM export")
    session.add_track("local", "Me", "microphone", 16000, 2)
    session.add_track("remote", "Other", "remote", 48000, 1)
    stereo = array.array(
        "h",
        (round(level * 32767 * math.sin(2 * math.pi * 440 * i / 16000)) for i in range(16000) for level in (0.4, 0.2)),
    )
    other = array.array("h", (round(0.1 * 32767 * math.sin(2 * math.pi * 880 * i / 48000)) for i in range(48000)))
    if sys.byteorder != "little":
        stereo.byteswap()
        other.byteswap()
    session.append("local", stereo.tobytes(), 250)
    session.append("remote", other.tobytes(), 0)
    session.finish()
    meeting = LibraryScanner(tmp_path).scan().meetings[0]
    before = {p.name: p.read_bytes() for p in session.directory.iterdir()}
    exported = export_meeting(meeting, tmp_path / "Documents")
    result, value = check_portable(exported)
    assert value.channels == 1 and abs(value.manifest["audio"]["sampleCount"] - 60000) <= 2
    assert value.manifest["audioEncode"]["bitrateBps"] == 64000 and value.manifest["audioEncode"]["cutoffHz"] == 20000
    assert result["words"] == []
    pipeline = Gst.parse_launch(
        "filesrc name=input ! oggdemux ! opusdec ! audioconvert ! audio/x-raw,format=F32LE,channels=1 ! appsink name=pcm sync=false"
    )
    pipeline.get_by_name("input").set_property("location", str(exported))
    pipeline.set_state(Gst.State.PLAYING)
    samples = array.array("f")
    try:
        sink = pipeline.get_by_name("pcm")
        while sample := sink.emit("try-pull-sample", 5 * Gst.SECOND):
            buffer = sample.get_buffer()
            samples.frombytes(buffer.extract_dup(0, buffer.get_size()))
    finally:
        pipeline.set_state(Gst.State.NULL)
    if sys.byteorder != "little":
        samples.byteswap()

    def magnitude(start, end, frequency):
        block = samples[round(start * 48000) : round(end * 48000)]
        return (
            2
            * abs(
                sum(
                    v
                    * complex(
                        math.cos(2 * math.pi * frequency * i / 48000), math.sin(2 * math.pi * frequency * i / 48000)
                    )
                    for i, v in enumerate(block)
                )
            )
            / len(block)
        )

    assert magnitude(0.05, 0.20, 440) < 0.02 and abs(magnitude(0.05, 0.20, 880) - 0.1) < 0.03
    assert abs(magnitude(0.4, 0.8, 440) - 0.3) < 0.05 and abs(magnitude(0.4, 0.8, 880) - 0.1) < 0.03
    assert all((session.directory / name).read_bytes() == data for name, data in before.items())


def test_header_page_shared_with_audio_is_rewritten_without_changing_packets(tmp_path):
    # The independent CC0 reference builder supplies CRC/framing, not our writer.
    build = runpy.run_path(str(ASSETS / "build.py"))
    with (ASSETS / "plain-title.opus").open("rb") as stream:
        head, _, _ = portable.read_header(stream)
    tags = b"OpusTags" + bytes(8)
    shared = tmp_path / "shared.opus"
    shared.write_bytes(build["page"]([head, tags] + [bytes.fromhex("f8fffe")] * 4, 0, 3192, 6))
    with shared.open("rb") as stream:
        before = portable.compute_audio_digest(stream)
    model = load_open_meeting(shared, tmp_path / "Library")
    exported = export_meeting(model, tmp_path / "Documents")
    _, value = check_portable(exported)
    assert value.manifest["integrity"]["opusAudioSha256"] == before["sha256"]
    assert value.manifest["audio"]["sampleCount"] == before["sampleCount"]


def test_null_words_import_as_none_and_remain_writable(tmp_path, monkeypatch):
    from cassini_talk import portable_writer

    original = portable_writer.chunk_set

    def null_body(prefix, body, mime):
        return original(prefix, dict(body, items=None) if prefix == "CASSINI_TX_WORDS_PAYLOAD_" else body, mime)

    monkeypatch.setattr(portable_writer, "chunk_set", null_body)
    model = load_open_meeting(ASSETS / "plain-title.opus", tmp_path / "Library")
    exported = export_meeting(model, tmp_path / "Documents")
    assert load_open_meeting(exported).transcript == "none"
    received = import_received(exported, tmp_path / "Library")
    assert received.transcript == "none" and received.can_write_down and not received.words


def test_empty_default_does_not_claim_all_transcripts_are_empty(tmp_path, monkeypatch):
    from cassini_talk import exporting
    from cassini_talk.portable_writer import MANIFEST_MIME, WORDS_MIME, chunk_set

    original = exporting.descriptors

    def multiple(*args, **kwargs):
        tags, manifest = original(*args, **kwargs)
        body = dict(
            format=portable.WORDS_FORMAT,
            wordCount=1,
            items=[dict(speaker="other", startMs=0, endMs=2, text="Alternate")],
        )
        extra, reference = chunk_set("CASSINI_TX_OTHER_PAYLOAD_", body, WORDS_MIME)
        manifest["speakers"].append(dict(id="other", label="Other"))
        manifest["transcripts"].append(
            dict(id="other", default=False, format=portable.WORDS_FORMAT, wordCount=1, payloadRef=reference)
        )
        tags.update(extra)
        tags.update(chunk_set("CASSINI_PAYLOAD_", manifest, MANIFEST_MIME)[0])
        tags["CASSINI_TRANSCRIPT_IDS"] = "words,other"
        return tags, manifest

    monkeypatch.setattr(exporting, "descriptors", multiple)
    model = load_open_meeting(ASSETS / "plain-title.opus", tmp_path / "Library")
    exported = export_meeting(model, tmp_path / "Documents")
    opened = load_open_meeting(exported)
    assert opened.transcript == "done" and not opened.words
    imported = import_received(exported, tmp_path / "Library")
    assert imported.transcript == "done" and imported.no_speech and imported.can_write_down


@pytest.mark.parametrize("name", ["clip.ogg", "clip.OPUS", "a\\b.opus", "clip.opus"])
def test_received_import_normalizes_copy_names_and_paths_without_changing_bytes(tmp_path, name):
    source = tmp_path / "outside\\folder" / name
    source.parent.mkdir()
    data = (ASSETS / "plain-title.opus").read_bytes()
    source.write_bytes(data)
    detour = source.parent / "sub"
    detour.mkdir()
    received = import_received(detour / ".." / name, tmp_path / "Library")
    assert received.audio_file.read_bytes() == data and source.read_bytes() == data
    assert received.audio_file.suffix == ".opus" and "\\" not in received.audio_file.name
    assert "originalFolderName" not in received.metadata["received"]
    assert received.metadata["received"]["originalName"] == received.audio_file.name
    assert len(LibraryScanner(tmp_path / "Library").scan().meetings) == 1
    assert not list((tmp_path / "Library").glob(".received-*"))


def test_received_import_rejects_oversized_notes_before_publication(tmp_path, monkeypatch):
    from cassini_talk import received

    source = ASSETS / "plain-title.opus"
    original = received.load_open_meeting
    notes = [dict(atMs=0, text="🙂" * 100000) for _ in range(3)]
    monkeypatch.setattr(received, "load_open_meeting", lambda *args: replace(original(*args), notes=notes))
    root = tmp_path / "Library"
    with pytest.raises(ValueError, match="exceeds limit"):
        import_received(source, root)
    assert not list(root.iterdir())


def test_received_import_cleans_published_folder_if_first_scan_fails(tmp_path, monkeypatch):
    from cassini_talk import received

    def fail(directory):
        raise ValueError("Synthetic scan failure")

    monkeypatch.setattr(received, "_meeting", fail)
    root = tmp_path / "Library"
    with pytest.raises(ValueError, match="Synthetic scan failure"):
        import_received(ASSETS / "plain-title.opus", root)
    assert not list(root.iterdir())


def test_recovered_session_exports_without_inventing_date_or_alignment(tmp_path):
    from datetime import UTC, datetime

    gst()
    session = RecordingSession(tmp_path, "Lost capture metadata")
    session.add_track("microphone", "Me", "microphone", 48000, 1)
    samples = array.array("h", (round(8000 * math.sin(2 * math.pi * 440 * i / 48000)) for i in range(48000)))
    if sys.byteorder != "little":
        samples.byteswap()
    session.append("microphone", samples.tobytes(), 1500)
    session.finish()
    (session.directory / "session.json").unlink()
    before = {p.name: p.read_bytes() for p in session.directory.iterdir()}
    meeting = LibraryScanner(tmp_path).scan().meetings[0]
    assert meeting.started_at_ms is None and all(t.start_offset_ms is None for t in meeting.tracks)
    start = datetime.now(UTC)
    exported = export_meeting(meeting, tmp_path / "Documents")
    stop = datetime.now(UTC)
    result, value = check_portable(exported)
    manifest = result["manifest"]
    created = datetime.fromisoformat(manifest["meeting"]["createdAtUtc"])
    assert start.timestamp() - 0.001 <= created.timestamp() <= stop.timestamp()
    assert "recordedAtLocal" not in manifest["meeting"]
    assert manifest["x"]["gocassini.com"]["recovery"] == {"offsetsAssumedZero": True}
    assert abs(value.manifest["audio"]["sampleCount"] - 48000) <= 2
    assert meeting.started_at_ms is None and all(t.start_offset_ms is None for t in meeting.tracks)
    assert {p.name: p.read_bytes() for p in session.directory.iterdir()} == before
    # Import/re-export carries the recovery namespace without interpreting it.
    received = load_open_meeting(exported, tmp_path / "Library")
    again = export_meeting(received, tmp_path / "Documents")
    second, _ = check_portable(again)
    assert second["manifest"]["x"] == manifest["x"]
    assert received.started_at_ms is not None and "recordedAtLocal" not in second["manifest"]["meeting"]


def test_export_stamp_is_current_and_known_meeting_start_is_only_recorded_at_local(tmp_path):
    from datetime import UTC, datetime

    from cassini_talk.portable_writer import descriptors

    meeting = replace(load_open_meeting(ASSETS / "plain-title.opus"), started_at_ms=1730611800000, origin="own")
    with meeting.audio_file.open("rb") as audio:
        digest = portable.compute_audio_digest(audio)
    before = datetime.now(UTC)
    tags, manifest = descriptors(meeting, digest, tz="America/New_York")
    after = datetime.now(UTC)
    created = datetime.fromisoformat(manifest["meeting"]["createdAtUtc"])
    assert before.timestamp() - 0.001 <= created.timestamp() <= after.timestamp()
    assert manifest["meeting"]["recordedAtLocal"] == "2024-11-03T01:30:00"
    assert tags["CASSINI_CREATED_AT"] == manifest["meeting"]["createdAtUtc"]
    assert "recovery" not in manifest.get("x", {}).get("gocassini.com", {})


def test_reexport_keeps_explicit_recording_label_and_never_promotes_import_date(tmp_path):
    from cassini_talk.portable_writer import descriptors, rewrite_tags

    model = replace(load_open_meeting(ASSETS / "plain-title.opus"), started_at_ms=1730611800000)
    with model.audio_file.open("rb") as audio:
        digest = portable.compute_audio_digest(audio)
        tags, plain = descriptors(model, digest)
        assert "recordedAtLocal" not in plain["meeting"], "Received Library date is not recording provenance"
        label = "2024-11-03T01:30:00"
        tags, _ = descriptors(model, digest, recorded_at_local=label)
        audio.seek(0)
        path = tmp_path / "labelled.opus"
        with path.open("wb") as output:
            rewrite_tags(audio, output, tags)
    received = load_open_meeting(path)
    exported = export_meeting(received, tmp_path / "Documents", tz="Asia/Tokyo")
    result, _ = check_portable(exported)
    assert result["manifest"]["meeting"]["recordedAtLocal"] == label


def test_received_import_rejects_final_symlink_before_copying(tmp_path):
    private = tmp_path / "private-target"
    private.mkdir()
    source = private / "original.ogg"
    source.write_bytes((ASSETS / "plain-title.opus").read_bytes())
    link = tmp_path / "clip.opus"
    link.symlink_to(source)
    root = tmp_path / "Library"
    with pytest.raises(OSError):
        import_received(link, root)
    assert not list(root.iterdir())
    assert source.read_bytes() == (ASSETS / "plain-title.opus").read_bytes()


@pytest.mark.parametrize("name", ["a" * 251 + ".ogg", "é" * 125 + ".ogg"])
def test_received_import_keeps_copy_basename_within_filesystem_limits(tmp_path, name):
    source = tmp_path / name
    data = (ASSETS / "plain-title.opus").read_bytes()
    source.write_bytes(data)
    root = tmp_path / "Library"
    meeting = import_received(source, root)
    assert meeting.has_audio and meeting.audio_file.read_bytes() == data
    assert len(meeting.audio_file.name.encode("utf-8")) <= 255
    assert meeting.metadata["received"]["originalName"] == meeting.audio_file.name
    assert not list(root.glob(".received-*"))


def test_received_dot_only_stem_is_rejected_before_library_creation(tmp_path):
    source = tmp_path / "..opus"
    source.write_bytes((ASSETS / "plain-title.opus").read_bytes())
    root = tmp_path / "Library"
    with pytest.raises(ValueError, match="Choose an Ogg Opus"):
        import_received(source, root)
    assert not root.exists()
