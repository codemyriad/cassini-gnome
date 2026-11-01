# SPDX-License-Identifier: GPL-3.0-or-later
"""D52.2 (P7): replay the shared gap vectors through the real writers and presence helper."""

import base64
import json
import math
from pathlib import Path

import pytest

from cassini_talk.capture_sources import BrowserStreams
from cassini_talk.native_timing import NativeTimeline
from cassini_talk.recording_session import BrowserHost
from cassini_talk.source_ownership import presence, samples

VECTORS = Path(__file__).parents[1] / "browser-protocol/conformance/gaps"
WRITER = json.loads((VECTORS / "writer.json").read_text())["cases"]
PRESENCE = json.loads((VECTORS / "presence.json").read_text())["cases"]
NEGOTIATED = "source-outcomes-v1"


class Replay:
    def __init__(self, tmp_path, case):
        self.negotiated = NEGOTIATED in case.get("features", ())
        self.host = BrowserHost(tmp_path)
        start = dict(type="start", captureId="gaps")
        if self.negotiated:
            start["features"] = [NEGOTIATED]
        assert self.host.handle(start)["type"] == "started"
        self.session = self.host.session
        selection = dict(dict(me=True, others=True), **case.get("selection", {}))
        if self.negotiated:
            self.host.audio_sink = BrowserStreams(self.session, **selection)
        self.sequences, self.timelines = {}, {}

    def track(self, id, kind="remote", rate=48000):
        declaration = dict(type="track", id=id, kind=kind, label="Guest", sampleRate=rate, channels=1)
        if self.negotiated:
            declaration["sourceTrackId"] = "root-" + id
        assert self.host.handle(declaration) == dict(type="ack")
        self.sequences[id] = 0

    def audio(self, id, offsetMs, frames):
        packet = dict(
            type="audio",
            id=id,
            sequence=self.sequences[id],
            offsetMs=offsetMs,
            pcm=base64.b64encode(b"\x01\x00" * frames).decode(),
        )
        assert self.host.handle(packet) == dict(type="ack")
        self.sequences[id] += 1

    def series(self, id, firstOffsetMs, everyMs, count, frames):
        for index in range(count):
            self.audio(id, firstOffsetMs + index * everyMs, frames)

    def select(self, atMs, me=None, others=None):
        sink = self.host.audio_sink
        sink.select(sink.me if me is None else me, sink.others if others is None else others, atMs)

    def trackEnd(self, id, droppedFrames=0):
        packet = dict(type="trackEnd", id=id, reason="ended", droppedFrames=droppedFrames, endOffsetMs=None)
        assert self.host.handle(packet) == dict(type="ack")

    def nativeTrack(self, id, kind="microphone", rate=48000, boundaryMs=None):
        self.session.add_track(id, kind, kind, rate, 1, label_source="device")
        if boundaryMs is not None:
            self.session.manifest["tracks"][-1]["startOffsetMs"] = boundaryMs
        self.timelines[id] = NativeTimeline(rate)

    def input(self, id, hostMs, frames):
        offset, gap = self.timelines[id].map(round(hostMs * 1_000_000), frames, hostMs)
        self.session.append_native(id, b"\x01\x00" * frames, offset, gap_frames=gap)

    def stop(self):
        stop = dict(type="stop", captureId="gaps") if self.negotiated else dict(type="stop")
        assert self.host.handle(stop)["type"] == "stopped"
        return json.loads((self.session.directory / "session.json").read_text())


def wire(track):
    return track["sourceTrackId"].removeprefix("root-") if "sourceTrackId" in track else track["id"]


def same_gaps(actual, expected):
    return len(actual) == len(expected) and all(
        a["kind"] == e["kind"]
        and a.get("frames") == e.get("frames")
        and math.isclose(a["atMs"], e["atMs"], abs_tol=1e-6)
        and math.isclose(a.get("durationMs", 0), e.get("durationMs", 0), abs_tol=1e-6)
        for a, e in zip(actual, expected, strict=True)
    )


@pytest.mark.parametrize("case", WRITER, ids=lambda case: case["name"])
def test_shared_writer_vectors_through_the_real_writers(tmp_path, case):
    replay = Replay(tmp_path, case)
    try:
        for step in case["steps"]:
            getattr(replay, step["op"])(**{key: value for key, value in step.items() if key != "op"})
        saved = replay.stop()
    finally:
        replay.host.close()
    expected = case["expected"]
    assert (saved["state"], saved.get("problemKind")) == (expected["state"], expected["problemKind"])
    assert [wire(t) for t in saved["tracks"]] == [t["wire"] for t in expected["tracks"]]
    for track, wanted in zip(saved["tracks"], expected["tracks"], strict=True):
        assert math.isclose(track["startOffsetMs"], wanted["startOffsetMs"], abs_tol=1e-6)
        assert track["frames"] == wanted["frames"]
        assert track.get("gapFrames", 0) == wanted["gapFrames"]
        assert track.get("droppedFrames", 0) == wanted["droppedFrames"]
        if "gaps" in wanted:
            assert same_gaps(track["gaps"], wanted["gaps"]), track["gaps"]
        else:
            assert len(track["gaps"]) == wanted["gapsCount"]
            assert same_gaps(track["gaps"][:2], wanted["gapsHead"])
            assert same_gaps(track["gaps"][-2:], wanted["gapsTail"])


@pytest.mark.parametrize("case", PRESENCE, ids=lambda case: case["name"])
def test_shared_presence_vectors(case):
    result = presence(case["track"], case.get("ownership"))
    assert samples(result.present) == case["expected"]["present"]
    assert samples(result.unknown) == case["expected"]["unknown"]
    stored = case["track"].get("gaps")
    assert result.complete == (isinstance(stored, list) and all(g["kind"] != "overflow" for g in stored))


def test_ranges_reach_the_capture_record_and_short_jumps_do_not_mark_the_library(tmp_path):
    from cassini_talk import capture_report
    from cassini_talk.library import scan_library

    case = next(c for c in WRITER if c["name"] == "short-jump-does-not-flag")
    replay = Replay(tmp_path, case)
    try:
        for step in case["steps"]:
            getattr(replay, step["op"])(**{key: value for key, value in step.items() if key != "op"})
        saved = replay.stop()
    finally:
        replay.host.close()
    record = capture_report.from_session(saved)
    assert (
        record["tracks"][0]["gaps"]
        == saved["tracks"][0]["gaps"]
        == [dict(atMs=1000, durationMs=25, frames=1200, kind="jump")]
    )
    (meeting,) = scan_library(tmp_path).meetings
    assert meeting.interrupted_at_ms is None, "a 25 ms jump is recorded but does not mark the meeting"


def test_native_gap_position_follows_the_host_clock_under_drift(tmp_path):
    from cassini_talk.capture_clock import mapped_end_sample
    from cassini_talk.recording_session import RecordingSession

    session = RecordingSession(tmp_path, "Drift")
    session.add_track("microphone", "Microphone", "microphone", 48000, 1, label_source="device")
    session.library_root = tmp_path  # enable host-clock anchors on this plain WAV writer
    timeline, origin = NativeTimeline(48000), session._native_clock_origin_ns
    first = host_ns = origin + 250_000_000
    # One hour at +50 ppm: the host clock runs ahead of the nominal sample clock.
    for _ in range(3600):
        offset, gap = timeline.map(host_ns, 48000, 250)
        session.append_native("microphone", b"\x01\x00" * 48000, offset, gap_frames=gap, host_ns=host_ns)
        host_ns += 1_000_050_000
    before = session.writers["microphone"].frames
    began = host_ns  # the previous buffer ended here on the host clock
    host_ns += 2_000_000_000  # a two-second input gap
    offset, gap = timeline.map(host_ns, 48000, 250)
    session.append_native("microphone", b"\x01\x00" * 48000, offset, gap_frames=gap, host_ns=host_ns)
    track = session.manifest["tracks"][0]
    (stored,) = track["gaps"]
    truth = track["startOffsetMs"] + (began - first) / 1_000_000
    anchors = session._native_clocks["microphone"].values()
    mapped = mapped_end_sample(before, 48000, track["startOffsetMs"], anchors) / 48
    nominal = track["startOffsetMs"] + before * 1000 / 48000
    assert stored["kind"] == "input" and stored["frames"] == gap
    assert abs(stored["atMs"] - truth) <= 0.1, (stored["atMs"], truth)
    # The stored anchors are pruned to one per minute, so Mapping interpolates
    # across the gap: it agrees within drift times the anchor spacing.
    assert abs(stored["atMs"] - mapped) <= 3, (stored["atMs"], mapped)
    assert stored["atMs"] - nominal > 150, "a nominal position would lag the drifted host clock"
    session.finish()


def jump(at, duration, frames):
    return dict(atMs=at, durationMs=duration, frames=frames, kind="jump")


def test_published_track_presence_takes_its_start_from_the_spans():
    gaps = [jump(2000, 1000, 48000)]
    session = dict(startOffsetMs=1000, sampleRate=48000, frames=192000, gaps=gaps)
    # x["codemyriad.io"].tracks[] carries gaps on the logical track and offsets only on spans.
    published = dict(
        id="a",
        kind="remote",
        gaps=gaps,
        gapFrames=48000,
        spans=[dict(file="track-a.wav", sampleRate=48000, channels=1, frames=192000, startOffsetMs=1000)],
    )
    rotated = dict(
        published,
        spans=[
            dict(file="track-a.wav", sampleRate=48000, channels=1, frames=96000, startOffsetMs=1000),
            dict(file="tracks/track-a-2.wav", sampleRate=48000, channels=1, frames=96000, startOffsetMs=3000),
        ],
    )
    expected = [[16000, 32000], [48000, 80000]]
    for track in (session, published, rotated):
        assert samples(presence(track).present) == expected, track
    unplaced = dict(published, spans=[dict(published["spans"][0], startOffsetMs=None)])
    assert presence(unplaced) is None


def test_spans_are_one_logical_extent_and_unplaced_or_empty_spans_add_nothing():
    span = dict(file="track-a.wav", sampleRate=48000, channels=1)
    rotated = dict(
        gaps=[],
        spans=[dict(span, frames=96000, startOffsetMs=1000), dict(span, frames=96000, startOffsetMs=3000)],
    )
    assert samples(presence(rotated).present) == [[16000, 80000]], "no split at the rotation boundary"
    mixed = dict(rotated, spans=rotated["spans"] + [dict(span, frames=48000, startOffsetMs=None)])
    assert samples(presence(mixed).present) == [[16000, 80000]], "an unplaced span is not assumed at 0"
    empty = dict(gaps=[], spans=[dict(span, frames=0, startOffsetMs=2000)])
    assert presence(empty).present == () and presence(empty).unknown == ()
    assert presence(dict(empty, gaps=[dict(atMs=None, kind="jump")])).present == ()


@pytest.mark.parametrize(
    "gaps,present,unknown",
    [
        ([dict(atMs=1000, kind="jump")], [[0, 16000]], [[16000, 32000]]),
        ([dict(atMs=None, durationMs=5, frames=240, kind="jump")], [], [[0, 32000]]),
        (["not a gap"], [], [[0, 32000]]),
        ([jump(500, 250, 12000), dict(atMs=1000, kind="jump")], [[0, 8000], [12000, 16000]], [[16000, 32000]]),
        ([jump(500, 250, 12000), dict(atMs="1000", durationMs=10, kind="jump")], [], [[0, 8000], [12000, 32000]]),
    ],
    ids=["no-duration", "no-position", "not-an-object", "after-a-valid-range", "unusable-position-after-a-range"],
)
def test_a_malformed_gap_entry_makes_the_rest_unknown(gaps, present, unknown):
    result = presence(dict(startOffsetMs=0, sampleRate=48000, frames=96000, gaps=gaps))
    assert (samples(result.present), samples(result.unknown), result.complete) == (present, unknown, False)


def test_null_gaps_read_as_an_older_writer_in_the_library(tmp_path):
    from test_library import capture

    from cassini_talk.library import scan_library

    session = capture(tmp_path)
    session.manifest["tracks"][0].update(gapFrames=480, gaps=None)
    (session.directory / "session.json").write_text(json.dumps(session.manifest))
    (meeting,) = scan_library(tmp_path).meetings
    assert meeting.interrupted_at_ms is not None


def test_full_gap_budgets_on_sixty_four_tracks_stay_readable(tmp_path):
    from cassini_talk.capture_facts import record_end
    from cassini_talk.capture_report import record_capture
    from cassini_talk.library import LibraryScanner
    from cassini_talk.library_publication import capture_parent
    from cassini_talk.meeting_store import read_metadata
    from cassini_talk.recording_session import GAP_BUDGET, METADATA_LIMIT

    root = tmp_path / "Library"
    host = BrowserHost(capture_parent(root))
    try:
        host.handle(dict(type="start", features=[NEGOTIATED], captureId="budget"))
        for index in range(64):
            identifier = f"t{index}"
            declaration = dict(type="track", id=identifier, sourceTrackId="root-" + identifier, kind="remote")
            assert host.handle(dict(declaration, label="Guest", sampleRate=48000, channels=1)) == dict(type="ack")
            pcm = base64.b64encode(b"\x01\x00" * 48000).decode()
            assert host.handle(dict(type="audio", id=identifier, sequence=0, offsetMs=0, pcm=pcm)) == dict(type="ack")
        # Worst-case float times on every track, at the full budget plus its marker.
        full = [jump(123456.78901234567 + n, 20.833333333333332, 1000) for n in range(GAP_BUDGET)]
        full.append(dict(atMs=999999.123456789, kind="overflow"))
        for track in host.session.manifest["tracks"]:
            track["gaps"] = [dict(gap) for gap in full]
        assert host.handle(dict(type="stop", captureId="budget"))["type"] == "stopped"
    finally:
        host.close()
    directory = host.session.directory
    raw = (directory / "session.json").read_bytes()
    assert 1 << 20 < len(raw) <= METADATA_LIMIT and b"\n  " not in raw, "a compact checkpoint past the old 1 MiB"
    saved = json.loads(raw)
    (meeting,) = LibraryScanner(root).scan().meetings
    assert meeting.interrupted_at_ms is None, "the Library read it, it did not salvage it"
    assert record_capture(directory, saved)
    assert (directory / "meeting.json").stat().st_size > 1 << 20
    record = read_metadata(directory)["recordingDetails"]["captureRecord"]
    assert len(record["tracks"]) == 64 and all(len(t["gaps"]) == GAP_BUDGET + 1 for t in record["tracks"])
    record_end(directory, "user")
    assert read_metadata(directory)["recordingDetails"]["end"]["reason"] == "user"


def test_a_checkpoint_during_padding_already_holds_its_range(tmp_path, monkeypatch):
    from cassini_talk.recording_session import RecordingSession

    session = RecordingSession(tmp_path, "Rotation")
    session.add_track("a", "Guest", "remote", 48000, 1, label_source="browser")
    session.append("a", b"\x01\x00" * 48000, 0)
    writer, seen = session.writers["a"], []
    silence = writer.silence

    def rotating(frames):
        # A WAV rotation checkpoints the manifest between padding blocks.
        silence(frames // 2)
        session.checkpoint()
        seen.append(json.loads((session.directory / "session.json").read_text())["tracks"][0]["gaps"])
        silence(frames - frames // 2)

    monkeypatch.setattr(writer, "silence", rotating)
    session.append("a", b"\x01\x00" * 48000, 3000)
    assert seen == [[jump(1000, 2000, 96000)]]
    assert session.manifest["tracks"][0]["gaps"] == [jump(1000, 2000, 96000)]
    session.finish()


def test_a_partly_written_native_gap_keeps_its_host_position(tmp_path, monkeypatch):
    from cassini_talk.recording_session import RecordingSession

    session = RecordingSession(tmp_path, "Partial")
    session.add_track("microphone", "Microphone", "microphone", 48000, 1, label_source="device")
    session.library_root = tmp_path  # enable host-clock anchors on this plain WAV writer
    timeline, origin = NativeTimeline(48000), session._native_clock_origin_ns
    host_ns = origin
    for _ in range(3):
        offset, gap = timeline.map(host_ns, 48000, 0)
        session.append_native("microphone", b"\x01\x00" * 48000, offset, gap_frames=gap, host_ns=host_ns)
        host_ns += 1_000_000_000
    host_ns += 2_000_000_000
    writer = session.writers["microphone"]
    silence = writer.silence

    def failing(frames):
        silence(frames // 4)
        raise OSError(28, "Generated ENOSPC")

    monkeypatch.setattr(writer, "silence", failing)
    offset, gap = timeline.map(host_ns, 48000, 0)
    with pytest.raises(OSError):
        session.append_native("microphone", b"\x01\x00" * 48000, offset, gap_frames=gap, host_ns=host_ns)
    (stored,) = session.manifest["tracks"][0]["gaps"]
    # The gap began 2 s before this buffer, however little of it reached the WAV.
    assert stored == dict(atMs=3000, durationMs=500, frames=24000, kind="input")
    assert session.manifest["tracks"][0]["gapFrames"] == 24000
