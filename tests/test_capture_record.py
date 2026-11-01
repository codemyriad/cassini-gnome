# SPDX-License-Identifier: GPL-3.0-or-later
"""Exercise real negotiated writers: every source stays accounted for independently."""

import base64
import errno
import json
import struct
from pathlib import Path

import pytest
from test_browser_host import invoke

from cassini_talk.capture_sources import BrowserStreams
from cassini_talk.recording_session import BrowserHost, ProtocolError, WAVWriter


def declare(identifier, root=None):
    return dict(
        type="track", id=identifier, kind="remote", label="Guest", sampleRate=48000, channels=1, sourceTrackId=root
    )


def pcm(identifier, frames, *, sequence=0, offset=0, value=b"\0\0"):
    return dict(
        type="audio",
        id=identifier,
        sequence=sequence,
        offsetMs=offset,
        pcm=base64.b64encode(value * frames).decode(),
    )


@pytest.fixture(params=["direct", "selected"])
def host(tmp_path, request):
    host = BrowserHost(tmp_path)
    started = host.handle(dict(type="start", features=["source-outcomes-v1"], captureId="capture-a"))
    assert started["features"] == ["source-outcomes-v1"]
    if request.param == "selected":
        host.audio_sink = BrowserStreams(host.session)
    yield host
    host.close()


def stop(host, **kwargs):
    return host.handle(dict(type="stop", captureId="capture-a", **kwargs))


OUTCOMES = json.loads(
    (Path(__file__).parents[1] / "browser-protocol/conformance/multistream/outcomes.json").read_text()
)["cases"]


@pytest.mark.parametrize("case", OUTCOMES, ids=lambda case: case["name"])
def test_shared_literal_outcomes_run_through_real_app_writers(host, case):
    # Use the fixture's wire rate, not a local reimplementation of its oracle.
    if sources := case.get("announcedSources"):
        assert host.handle(dict(type="sources", discoveredSources=sources)) == dict(type="ack")
        durable = json.loads((host.session.directory / "session.json").read_text())
        assert {s["sourceTrackId"] for s in durable["captureOutcome"]["sources"]} == {
            s["sourceTrackId"] for s in sources
        }
        assert durable["tracks"] == []
    for track in case["tracks"]:
        assert host.handle(dict(declare(track["id"], track["sourceTrackId"]), sampleRate=8000)) == dict(type="ack")
    sequences = dict.fromkeys((t["id"] for t in case["tracks"]), 0)
    for write in case["writes"]:
        identifier = write["id"]
        assert host.handle(
            pcm(
                identifier,
                write["frames"],
                sequence=sequences[identifier],
                offset=write["offsetMs"],
                value=struct.pack("<h", write["value"]),
            )
        ) == dict(type="ack")
        sequences[identifier] += 1
    assert stop(host, discoveredSources=case.get("discoveredSources", []))["type"] == "stopped"
    manifest = json.loads((host.session.directory / "session.json").read_text())
    assert {t["id"]: t["captureOutcome"] for t in manifest["tracks"]} == case["expected"]
    assert {s["sourceTrackId"]: s["captureOutcome"] for s in manifest["captureOutcome"]["sources"]} == case[
        "sourceExpected"
    ]
    for track in manifest["tracks"]:
        assert track["receivedFrames"] == sum(w["frames"] for w in case["writes"] if w["id"] == track["id"])


@pytest.mark.parametrize("b_frames,expected", [(0, "neverAudio"), (24000, "lowAudio")])
def test_announced_missing_low_and_zero_pcm_sources_are_distinct(host, b_frames, expected):
    for identifier in ("a", "b", "zeros"):
        assert host.handle(declare(identifier))["type"] == "ack"
    host.handle(pcm("a", 24000))
    host.handle(pcm("a", 24000, sequence=1, offset=1500))
    if b_frames:
        host.handle(pcm("b", b_frames))
    host.handle(pcm("zeros", 48000))
    assert stop(host)["type"] == "stopped"
    manifest = json.loads((host.session.directory / "session.json").read_text())
    sources = {s["sourceTrackId"]: s for s in manifest["captureOutcome"]["sources"]}
    assert sources["a"]["captureOutcome"] == "captured" and sources["a"]["audioMs"] == 1000
    assert sources["b"]["captureOutcome"] == expected
    assert sources["zeros"]["captureOutcome"] == "captured"
    first = manifest["tracks"][0]
    assert first["frames"] == 96000 and first["receivedFrames"] == 48000
    assert manifest["problemKind"] == "capture" and manifest["state"] == "interrupted"


def test_repeated_declaration_keeps_sequence_and_track_end_uses_committed_samples(host):
    host.handle(declare("one", "receiver"))
    host.handle(pcm("one", 48000))
    assert host.handle(declare("one", "receiver")) == dict(type="ack")
    assert host.sequences["one"] == 1 and len(host.session.manifest["tracks"]) == 1
    assert host.handle(dict(type="trackEnd", id="one", reason="replaced", droppedFrames=0, endOffsetMs=999999)) == dict(
        type="ack"
    )
    assert host.session.manifest["tracks"][0]["endOffsetMs"] == 1000
    host.handle(declare("two", "receiver"))
    assert host.handle(pcm("two", 48000, offset=1000)) == dict(type="ack")
    stop(host)
    (source,) = host.session.manifest["captureOutcome"]["sources"]
    assert source["trackIds"] == ["one", "two"] and source["audioMs"] == 2000
    assert source["captureOutcome"] == "captured"


def test_live_and_stored_overlap_refuse_only_the_offending_source(host):
    host.handle(declare("one", "receiver"))
    host.handle(pcm("one", 48000))
    live = host.handle(declare("live-overlap", "receiver"))
    assert live["type"] == "error" and live["scope"] == "source" and live["id"] == "live-overlap"
    host.handle(dict(type="trackEnd", id="one", reason="ended", droppedFrames=0, endOffsetMs=None))
    host.handle(declare("stored-overlap", "receiver"))
    overlap = host.handle(pcm("stored-overlap", 48000, offset=500))
    assert overlap["type"] == "error" and overlap["scope"] == "source"
    host.handle(declare("unrelated", "different"))
    assert host.handle(pcm("unrelated", 48000)) == dict(type="ack")
    stop(host)
    tracks = host.session.manifest["tracks"]
    assert len(tracks) == 3 and tracks[1]["frames"] == 0 and tracks[2]["frames"] == 48000


def test_source_io_failure_is_sticky_but_another_writer_and_a_fresh_span_continue(host, monkeypatch):
    host.handle(declare("a", "receiver-a"))
    host.handle(declare("b", "receiver-b"))
    session = host.session
    writer = session.writers["b"]

    def no_space(_data):
        raise OSError(errno.ENOSPC, "Generated source failure")

    monkeypatch.setattr(writer, "append", no_space)
    reply = host.handle(pcm("b", 48000))
    assert reply["scope"] == "source" and reply["code"] == "io" and reply["id"] == "b"
    assert not session.finished
    assert host.handle(pcm("a", 48000)) == dict(type="ack")
    assert host.handle(declare("b-resumed", "receiver-b")) == dict(type="ack")
    assert host.handle(pcm("b-resumed", 48000, offset=1000)) == dict(type="ack")
    stop(host)
    assert session.manifest["problemKind"] == "io"
    assert session.manifest["tracks"][1]["droppedFrames"] == 48000
    sources = {s["sourceTrackId"]: s for s in session.manifest["captureOutcome"]["sources"]}
    assert sources["receiver-a"]["captureOutcome"] == "captured"
    assert sources["receiver-b"]["captureOutcome"] == "failed" and sources["receiver-b"]["audioMs"] == 1000


def test_failed_wav_creation_leaves_no_orphan_and_allows_a_fresh_source_span(host, monkeypatch):
    def no_header(_self):
        raise OSError(errno.ENOSPC, "Generated WAV header failure")

    with monkeypatch.context() as patch:
        patch.setattr(WAVWriter, "_header", no_header)
        refused = host.handle(declare("failed", "receiver"))
    assert refused["code"] == "io" and refused["scope"] == "source"
    assert not host.session.writers and not list(host.session.directory.glob("*.wav"))
    assert host.handle(declare("fresh", "receiver")) == dict(type="ack")
    assert host.handle(pcm("fresh", 48000)) == dict(type="ack")
    stop(host)
    (source,) = host.session.manifest["captureOutcome"]["sources"]
    assert source["captureOutcome"] == "failed" and source["trackIds"] == ["fresh"]
    assert host.session.manifest["problemKind"] == "io"


@pytest.mark.parametrize("phase", ["padding", "content"])
def test_partial_source_write_retains_exact_gap_and_received_counts(host, monkeypatch, phase):
    import wave

    host.handle(declare("healthy", "receiver-a"))
    host.handle(declare("broken", "receiver-b"))
    assert host.handle(pcm("broken", 48000, value=b"\x11\x11")) == dict(type="ack")
    writer = host.session.writers["broken"]
    append = writer.append
    gap_frames = 8192 if phase == "padding" else 96000
    received_frames = 0 if phase == "padding" else 12000

    def partial_padding(_frames):
        append(bytes(gap_frames * 2))
        raise OSError(errno.ENOSPC, "Generated failure during timestamp padding")

    def partial_content(data):
        append(data[: received_frames * 2])
        raise OSError(errno.ENOSPC, "Generated failure after partial source PCM")

    with monkeypatch.context() as patch:
        patch.setattr(
            writer,
            "silence" if phase == "padding" else "append",
            partial_padding if phase == "padding" else partial_content,
        )
        # On the content path the original silence() also calls append().
        if phase == "content":
            patch.setattr(writer, "silence", lambda frames: append(bytes(frames * 2)))
        refused = host.handle(pcm("broken", 48000, sequence=1, offset=3000, value=b"\x22\x22"))
    assert refused["scope"] == "source" and refused["code"] == "io"
    assert host.handle(pcm("healthy", 48000)) == dict(type="ack")
    assert host.handle(declare("recovered", "receiver-b")) == dict(type="ack")
    assert host.handle(pcm("recovered", 48000, offset=5000)) == dict(type="ack")
    stop(host)
    manifest = json.loads((host.session.directory / "session.json").read_text())
    track = next(t for t in manifest["tracks"] if t["id"] == "broken")
    assert track["gapFrames"] == gap_frames
    assert track["receivedFrames"] == 48000 + received_frames
    assert track["droppedFrames"] == 48000 - received_frames
    assert track["frames"] == 48000 + gap_frames + received_frames
    with wave.open(str(host.session.directory / track["file"]), "rb") as saved:
        assert saved.getnframes() == track["frames"]
        data = saved.readframes(saved.getnframes())
    assert data == b"\x11\x11" * 48000 + bytes(gap_frames * 2) + b"\x22\x22" * received_frames
    sources = {s["sourceTrackId"]: s for s in manifest["captureOutcome"]["sources"]}
    assert sources["receiver-a"]["captureOutcome"] == "captured"
    assert sources["receiver-b"]["captureOutcome"] == "failed"
    assert sources["receiver-b"]["audioMs"] == (96000 + received_frames) / 48
    assert manifest["problemKind"] == "io"


def test_refused_and_discovered_sources_do_not_allocate_wavs(host):
    session = host.session
    for index in range(64):
        assert host.handle(declare(f"wire-{index}"))["type"] == "ack"
    refused = host.handle(declare("sixty-fifth"))
    assert refused["scope"] == "source" and refused["code"] == "limit"
    assert len(session.writers) == 64
    assert len(list(session.directory.glob("*.wav"))) == 64
    checkpoint = json.loads((session.directory / "session.json").read_text())
    assert checkpoint["captureOutcome"]["sources"][-1]["sourceTrackId"] == "sixty-fifth"
    discovered = [dict(sourceTrackId=f"unopened-{i}", kind="remote", failed=True) for i in range(257)]
    assert stop(host, discoveredSources=discovered)["type"] == "stopped"
    assert len(session.manifest["captureOutcome"]["sources"]) == 322
    assert len(session.writers) == 64


@pytest.mark.parametrize("mode", ["off", "off-failed", "previously-on", "later-on"])
def test_intentionally_unselected_source_is_not_a_capture_problem(tmp_path, mode):
    host = BrowserHost(tmp_path)
    host.handle(dict(type="start", features=["source-outcomes-v1"], captureId="capture-a"))
    host.audio_sink = BrowserStreams(host.session, me=False, others=mode == "previously-on")
    session = host.session
    try:
        session.add_track("native-mic", "Me", "microphone", 48000, 1, label_source="device")
        session.append("native-mic", b"\x01\x00" * 48000, 0)
        host.handle(
            dict(
                type="sources",
                discoveredSources=[dict(sourceTrackId="unselected", kind="remote", failed=mode == "off-failed")],
            )
        )
        assert host.handle(declare("browser", "unselected")) == dict(type="ack")
        host.audio_sink.select(False, False, 0)
        assert host.handle(pcm("browser", 48000)) == dict(type="ack")
        if mode == "later-on":
            host.audio_sink.select(False, True, 1000)
            assert host.handle(pcm("browser", 48000, sequence=1, offset=1000)) == dict(type="ack")
        stop(host)
        (row,) = session.manifest["captureOutcome"]["sources"]
        if mode == "previously-on":
            assert row.get("captureExpected", True) and row["captureOutcome"] == "neverAudio"
            assert session.manifest["state"] == "interrupted"
        elif mode == "later-on":
            assert row.get("captureExpected", True) and row["captureOutcome"] == "captured"
            assert row["audioMs"] == 1000 and session.manifest["state"] == "completed"
        else:
            assert row["captureExpected"] is False
            assert row["captureOutcome"] == ("failed" if mode == "off-failed" else "neverAudio")
            assert row["trackIds"] == [] and row["audioMs"] == 0
            assert session.manifest["state"] == "completed" and "problemKind" not in session.manifest
    finally:
        host.close()


def test_browser_cannot_suppress_an_expected_source_warning(host):
    assert host.handle(
        dict(type="sources", discoveredSources=[dict(sourceTrackId="expected", kind="remote", captureExpected=False)])
    ) == dict(type="ack")
    stop(host)
    (row,) = host.session.manifest["captureOutcome"]["sources"]
    assert row.get("captureExpected", True) is True and row["captureOutcome"] == "neverAudio"
    assert host.session.manifest["state"] == "interrupted" and host.session.manifest["problemKind"] == "capture"


def test_browser_wire_id_cannot_retire_or_claim_native_microphone_pcm(host):
    session = host.session
    session.add_track("native-mic", "Me", "microphone", 48000, 1, label_source="device")
    refused = host.handle(declare("native-mic"))
    if host.audio_sink is None:
        assert refused["type"] == "error" and refused["scope"] == "source"
    else:
        # Selected capture renames the WAV span instead of refusing a wire ID
        # already owned by the native microphone. Its accounting stays separate.
        assert refused["type"] == "ack"
        assert host.handle(pcm("native-mic", 4800)) == dict(type="ack")
    native = session.manifest["tracks"][0]
    assert not native.get("ended") and "problemKind" not in native
    session.append_native("native-mic", b"\x01\x00" * 48000, 0)
    stop(host)
    (source,) = session.manifest["captureOutcome"]["sources"]
    assert "native-mic" not in source["trackIds"]
    assert source["audioMs"] == (100 if host.audio_sink is not None else 0)
    assert native["frames"] == 48000


def test_failed_source_with_others_off_is_checkpointed_without_a_wav(tmp_path):
    host = BrowserHost(tmp_path)
    host.handle(dict(type="start", features=["source-outcomes-v1"]))
    host.audio_sink = BrowserStreams(host.session, others=False)
    host.handle(declare("off", "receiver"))
    host.handle(dict(type="trackEnd", id="off", reason="failed", droppedFrames=0, endOffsetMs=None))
    saved = json.loads((host.session.directory / "session.json").read_text())
    assert saved["tracks"] == []
    assert saved["captureOutcome"]["sources"][0]["captureOutcome"] == "failed"
    host.handle(dict(type="stop"))


@pytest.mark.parametrize(
    "features", [None, "source-outcomes-v1", ["duplicate"] * 2, list("123456789"), ["x" * 65], [{}]]
)
def test_invalid_negotiation_allocates_no_session(tmp_path, features):
    host = BrowserHost(tmp_path)
    with pytest.raises(ProtocolError, match="Invalid recording features"):
        host.handle(dict(type="start", features=features))
    assert host.session is None and not list(tmp_path.iterdir())


def test_wrong_capture_stop_and_retired_audio_do_not_end_or_damage_the_session(host):
    host.handle(declare("one"))
    host.handle(pcm("one", 48000))
    wrong = host.handle(dict(type="stop", captureId="another-capture"))
    assert wrong["scope"] == "command" and not host.session.finished
    host.handle(dict(type="trackEnd", id="one", reason="stopped", droppedFrames=0, endOffsetMs=None))
    late = host.handle(pcm("one", 1, sequence=1, offset=1000))
    assert late["scope"] == "source"
    stop(host)
    assert host.session.manifest["state"] == "completed"
    before = (host.session.directory / "session.json").read_bytes()
    assert host.handle(pcm("one", 1, sequence=1, offset=1000))["scope"] == "source"
    assert (host.session.directory / "session.json").read_bytes() == before
    assert not host.session.capture_record.io_failed


def test_shared_manifest_io_failure_is_fatal_instead_of_a_source_refusal(host, monkeypatch):
    host.handle(declare("one"))
    host.session._last_checkpoint = -1000

    def broken_checkpoint():
        raise OSError(errno.EIO, "Generated shared manifest failure")

    with monkeypatch.context() as patch:
        patch.setattr(host.session, "checkpoint", broken_checkpoint)
        with pytest.raises(OSError, match="shared manifest"):
            host.handle(pcm("one", 48000))
    host.close("Generated shared manifest failure", problem_kind="io")
    assert host.session.manifest["problemKind"] == "io"


def test_stop_records_the_specific_writer_that_failed_its_final_sync(host, monkeypatch):
    host.handle(declare("one"))
    host.handle(pcm("one", 48000))
    writer = host.session.writers["one"]

    def broken_sync():
        if not writer.file.closed:
            raise OSError(errno.EIO, "Generated writer sync failure")

    monkeypatch.setattr(writer, "sync", broken_sync)
    with pytest.raises(OSError, match="writer sync"):
        stop(host)
    saved = json.loads((host.session.directory / "session.json").read_text())
    assert saved["problemKind"] == "io"
    assert saved["captureOutcome"]["sources"][0]["captureOutcome"] == "failed"
    assert saved["tracks"][0]["problemKind"] == "io"


def test_negotiated_stdio_refusal_leaves_the_connection_usable(tmp_path):
    code, replies = invoke(
        tmp_path,
        [
            dict(type="start", features=["source-outcomes-v1"]),
            declare("one", "receiver"),
            pcm("one", 48000),
            declare("overlap", "receiver"),
            declare("other", "other-receiver"),
            pcm("other", 48000),
            dict(type="stop"),
        ],
        open_completed=False,
    )
    assert code == 0
    assert replies[3]["scope"] == "source"
    assert [r["type"] for r in replies] == ["started", "ack", "ack", "error", "ack", "ack", "stopped"]


def test_live_discovery_is_durable_before_native_stop_and_late_updates_cannot_rewrite_it(host):
    source = dict(sourceTrackId="never-opened", kind="remote", label="Guest", failed=True)
    assert host.handle(dict(type="sources", discoveredSources=[source])) == dict(type="ack")
    path = host.session.directory / "session.json"
    before = json.loads(path.read_text())
    assert before["tracks"] == [] and before["captureOutcome"]["sources"][0]["captureOutcome"] == "failed"
    host.session.finish(reason="user")  # The app stops without waiting for the extension.
    final = path.read_bytes()
    assert stop(host)["type"] == "stopped"
    late = host.handle(dict(type="sources", discoveredSources=[dict(source, sourceTrackId="too-late")]))
    assert late["scope"] == "command" and late["code"] == "state"
    assert path.read_bytes() == final and "too-late" not in host.session.capture_record.seen
    late_summary = stop(host, discoveredSources=[dict(source, sourceTrackId="late-summary")])
    assert late_summary["scope"] == "command" and path.read_bytes() == final


@pytest.mark.parametrize(
    "bad",
    [
        dict(sourceTrackId="é" * 101, kind="remote"),
        dict(sourceTrackId="bad", kind="system"),
        dict(sourceTrackId="bad", kind="remote", label=None),
        dict(sourceTrackId="bad", kind="remote", label="x" * 201),
    ],
)
def test_source_batch_validation_is_atomic_and_other_audio_continues(host, bad):
    before = (host.session.directory / "session.json").read_bytes()
    refused = host.handle(
        dict(type="sources", discoveredSources=[dict(sourceTrackId="must-not-stick", kind="remote"), bad])
    )
    assert refused["scope"] == "command" and refused["code"] == "source"
    assert not host.session.capture_record.seen
    assert (host.session.directory / "session.json").read_bytes() == before
    host.handle(declare("healthy"))
    assert host.handle(pcm("healthy", 48000)) == dict(type="ack")
    stop(host)
    assert host.session.manifest["state"] == "completed"


def test_late_span_packets_are_refused_without_failing_the_replacement(host):
    host.handle(declare("old", "receiver"))
    host.handle(pcm("old", 48000))
    host.handle(dict(type="trackEnd", id="old", reason="ended", droppedFrames=0, endOffsetMs=None))
    host.handle(declare("new", "receiver"))
    host.handle(pcm("new", 48000, offset=1000))
    before = (host.session.directory / "session.json").read_bytes()
    for packet in [
        declare("old", "receiver"),
        pcm("old", 48000, offset=1000, sequence=1),
        dict(type="trackEnd", id="old", reason="failed", droppedFrames=123, endOffsetMs=2000),
    ]:
        reply = host.handle(packet)
        assert reply["type"] == "error" and reply["scope"] == "source"
        assert (host.session.directory / "session.json").read_bytes() == before
    assert stop(host)["type"] == "stopped"
    assert host.session.manifest["captureOutcome"]["sources"][0]["captureOutcome"] == "captured"
