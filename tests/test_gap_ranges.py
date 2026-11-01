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
