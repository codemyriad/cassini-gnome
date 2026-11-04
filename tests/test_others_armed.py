# SPDX-License-Identifier: GPL-3.0-or-later
"""D58.1: the others' side (computer sound) stays armed through silence.

It is never refused for silence or a missing buffer: it opens on its first
buffer however late, and only an explicit error refuses it, with its own D58
code. The microphone keeps its 3 s bound. The devices are scripted appsrc
branches on a fake open clock (test_native_sides.Sides): no sound server, and
no wall-time wait stands in for a device event.
"""

import json
import logging
import struct
import threading
import time

import pytest

gi = pytest.importorskip("gi")
try:
    gi.require_version("Gst", "1.0")
except ValueError:
    pytest.skip("GStreamer typelib is unavailable", allow_module_level=True)
from gi.repository import GLib, Gst  # noqa: E402
from test_native_sides import BOUND, CAUSES, Sides, browser_start, close, kinds_of, native_pcm, refusals  # noqa: E402
from test_recorder import spin_until  # noqa: E402

from cassini_talk import refusals as R  # noqa: E402
from cassini_talk.native_recorder import NativeRecorder  # noqa: E402

Gst.init(None)
DENIED = Gst.ResourceError.NOT_AUTHORIZED
QUIET = {"microphone": ("ok", 0.1), "system": ("never", 0)}


def start(tmp_path, plan, causes=None, codes=None):
    recorder = NativeRecorder()
    sides = Sides(recorder, plan, causes, codes)
    signals = []
    for name in ("started", "error", "finished"):
        recorder.connect(name, lambda _r, value, name=name: signals.append((name, value)))
    recorder.start(tmp_path, test=True)
    spin_until(lambda: not recorder.starting)
    return recorder, sides, signals


def names(signals):
    return [name for name, _ in signals]


def live(session):
    manifest = session.manifest
    tracks = [(t["kind"], t.get("ended"), t.get("startOffsetMs")) for t in manifest["tracks"]]
    return R.live_lines(manifest.get("refusals"), tracks, manifest.get("events") or ())


def post_error(sides, kind, code, cause="Fake monitor went away"):
    """An explicit device error on a side's current branch, then its stream's end."""
    source = sides.sources[kind]
    for problem in (cause, "Internal data stream error."):
        error = GLib.Error.new_literal(Gst.ResourceError.quark(), problem, int(code))
        source.post_message(Gst.Message.new_error(source, error, "scripted"))
    source.emit("end-of-stream")


def saved(session):
    return json.loads((session.directory / "session.json").read_text())


def stop(recorder, signals):
    recorder.stop()
    spin_until(lambda: "finished" in names(signals) and recorder.pipeline is None and not recorder._finishing)


def test_a_silent_others_side_stays_armed_and_opens_on_its_late_first_buffer(tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger="cassini_talk.native_recorder")
    recorder, sides, signals = start(tmp_path, QUIET)
    try:
        session = recorder.session
        # The start is decided at the others' 3 s window: no refusal, no track yet.
        assert names(signals) == ["started"] and sides.started_at == BOUND
        assert kinds_of(session) == ["microphone"] and "refusals" not in session.manifest
        assert recorder.refusals == [] and not refusals(caplog) and live(session) == []
        assert recorder._pending_sources["system"]["armed"]
        # Much later on the open clock (well past any bound), the computer sounds.
        sides.now = BOUND + 7.5
        assert sides.fire("system") == Gst.FlowReturn.OK
        spin_until(lambda: "system" in recorder._track_ids)
        spin_until(lambda: session.writers["system"].frames == 480)  # That first buffer is recorded.
        assert kinds_of(session) == ["microphone", "system"] and not recorder._pending_sources
        assert [t["file"] for t in session.manifest["tracks"]] == ["track-microphone.wav", "track-system.wav"]
        system = session.manifest["tracks"][1]
        assert system["startOffsetMs"] >= session.manifest["tracks"][0]["startOffsetMs"]
        sides.feed("system", 9)
        sides.feed("microphone", 10)
        spin_until(lambda: session.writers["system"].frames == 4800)
        assert live(session) == [] and "refusals" not in session.manifest
    finally:
        close(recorder)
    stored = saved(session)
    assert stored["state"] == "completed" and "problem" not in stored and "refusals" not in stored
    assert [t["kind"] for t in stored["tracks"]] == ["microphone", "system"]
    assert recorder.unheard == set() and not refusals(caplog)


def test_others_silent_until_stop_leave_no_track_no_refusal_and_no_failure_line(tmp_path, caplog):
    from cassini_talk.capture_details import read_details
    from cassini_talk.library import LibraryScanner

    caplog.set_level(logging.WARNING, logger="cassini_talk.native_recorder")
    recorder, sides, signals = start(tmp_path, QUIET)
    session = recorder.session
    try:
        sides.feed("microphone", 99)
        spin_until(lambda: session.writers["microphone"].frames == 48000)
        assert live(session) == [] and recorder.refusals == []
        stop(recorder, signals)
    finally:
        close(recorder)
    # The folder: a microphone track only, no refusal, a completed recording.
    assert names(signals) == ["started", "finished"] and not recorder.discarded
    stored = saved(session)
    assert [t["kind"] for t in stored["tracks"]] == ["microphone"]
    assert "refusals" not in stored and stored["state"] == "completed" and "problem" not in stored
    assert [e for e in stored["events"] if e["type"] != "end"] == [
        dict(atMs=0, type="othersSource", source="system", reason="missing"),
        dict(atMs=0, type="selection", me=True, others=True, reason="start"),
    ]
    assert not (session.directory / "track-system.wav").exists()
    assert recorder.unheard == {"system"} and not refusals(caplog)
    # Details: no refusal or failure line, nor any permission copy; The others
    # simply have no saved audio.
    meeting = next(m for m in LibraryScanner(tmp_path).scan().meetings if m.directory == session.directory)
    report = read_details(meeting)
    assert report.refusals == ()
    assert not any(
        line.warning or line.id.startswith(("details.sources.refused", "details.sources.failed"))
        for line in report.audio
    )
    copied = report.plain_text()
    assert "couldn’t be started" not in copied and "ermission" not in copied
    assert [s.side for s in report.spans] == ["Me"]


@pytest.mark.parametrize(
    ("kind", "code", "expected"),
    [
        ("system", DENIED, "permissionDenied"),
        ("system", Gst.ResourceError.OPEN_READ, "deviceUnavailable"),
        ("system", Gst.ResourceError.NOT_FOUND, "deviceUnavailable"),
        # The microphone keeps its D58 classification.
        ("microphone", DENIED, "deviceUnavailable"),
    ],
)
def test_an_explicit_error_within_the_window_refuses_with_its_own_code(tmp_path, kind, code, expected):
    other = "microphone" if kind == "system" else "system"
    recorder, _sides, signals = start(tmp_path, {kind: ("error", 0.5), other: ("ok", 0.1)}, CAUSES, {kind: code})
    try:
        session = recorder.session
        assert names(signals) == ["started"] and kinds_of(session) == [other]
        assert [(r["side"], r["code"], r["atMs"]) for r in session.manifest["refusals"]] == [
            (R.SIDES[kind], expected, 0)
        ]
    finally:
        close(recorder)


def test_a_failed_state_change_refuses_the_others_as_unavailable(tmp_path):
    recorder, _sides, _signals = start(tmp_path, {"microphone": ("ok", 0.1), "system": ("fail", 0)})
    try:
        assert recorder.session.manifest["refusals"] == [
            dict(atMs=0, side="others", code="deviceUnavailable", message="Could not start native audio capture")
        ]
    finally:
        close(recorder)


@pytest.mark.parametrize(
    ("code", "expected"), [(DENIED, "permissionDenied"), (Gst.ResourceError.BUSY, "deviceUnavailable")]
)
def test_an_explicit_error_after_the_window_refuses_an_armed_side_at_its_start(tmp_path, caplog, code, expected):
    caplog.set_level(logging.WARNING, logger="cassini_talk.native_recorder")
    recorder, sides, signals = start(tmp_path, QUIET)
    try:
        session = recorder.session
        assert live(session) == []
        sides.now = BOUND + 20
        post_error(sides, "system", code)
        spin_until(lambda: "refusals" in session.manifest and not recorder._pending_sources)
        assert [(r["atMs"], r["side"], r["code"]) for r in session.manifest["refusals"]] == [(0, "others", expected)]
        assert recorder.refusals == session.manifest["refusals"]
        # Only now is there a line for the others; the microphone records on.
        assert [line["id"] for line in live(session)] == ["errors.deviceUnavailable.others"]
        assert names(signals) == ["started"] and not recorder._finishing
        sides.feed("microphone", 10)
        spin_until(lambda: session.writers["microphone"].frames == 5280)
        assert refusals(caplog) == [
            "Native others side (system) did not open: " + session.manifest["refusals"][0]["message"]
        ]
        stop(recorder, signals)
    finally:
        close(recorder)
    assert saved(session)["state"] == "completed" and names(signals) == ["started", "finished"]


def test_a_refused_microphone_with_silent_others_records_until_stop_then_leaves_no_folder(tmp_path):
    recorder, sides, signals = start(tmp_path, {"microphone": ("fail", 0), "system": ("never", 0)})
    try:
        session = recorder.session
        # Armed others: a recording, with the microphone's line only.
        assert names(signals) == ["started"] and kinds_of(session) == []
        assert [(r["side"], r["code"]) for r in session.manifest["refusals"]] == [("me", "deviceUnavailable")]
        assert [line["side"] for line in live(session)] == ["me"]
        stop(recorder, signals)
        # Nothing ever opened (D58 point 2): no folder, and the start banner names
        # only the refused microphone, never the silent others.
        assert recorder.discarded and not session.directory.exists() and not list(tmp_path.iterdir())
        assert names(signals) == ["started", "error", "finished"] and signals[-1] == ("finished", "")
        banner = R.banner(recorder.refusals)
        assert [(r["side"], r["code"]) for r in banner["refusals"]] == [("me", "deviceUnavailable")]
        assert recorder.unheard == {"system"}
    finally:
        close(recorder)


def test_a_refused_microphone_and_late_sounding_others_keep_the_recording(tmp_path):
    recorder, sides, signals = start(tmp_path, {"microphone": ("fail", 0), "system": ("never", 0)})
    try:
        session = recorder.session
        sides.now = BOUND + 10
        sides.fire("system")
        spin_until(lambda: session.writers.get("system") is not None and session.writers["system"].frames == 480)
        assert live(session) == [dict(side="me", id="errors.deviceUnavailable.me", caption=None)]
        stop(recorder, signals)
    finally:
        close(recorder)
    stored = saved(session)
    assert [t["kind"] for t in stored["tracks"]] == ["system"] and stored["state"] == "completed"
    assert [(r["side"], r["code"]) for r in stored["refusals"]] == [("me", "deviceUnavailable")]
    assert not recorder.discarded and names(signals) == ["started", "finished"]


def test_a_refused_microphone_and_a_late_others_error_record_nothing(tmp_path):
    recorder, sides, signals = start(tmp_path, {"microphone": ("fail", 0), "system": ("never", 0)})
    try:
        session = recorder.session
        post_error(sides, "system", DENIED)
        spin_until(lambda: "finished" in names(signals))
        assert recorder.discarded and not session.directory.exists()
        assert [(r["side"], r["code"]) for r in recorder.refusals] == [
            ("me", "deviceUnavailable"),
            ("others", "permissionDenied"),
        ]
        assert names(signals) == ["started", "error", "finished"]
    finally:
        close(recorder)


def turn_on(recorder, me, others):
    future = recorder.select(me, others)
    spin_until(lambda: future.done() and not recorder._changing_sources)
    return future.result()


@pytest.fixture
def quiet_turn_on():
    """A turn-on's window passes without a buffer (the fake clock, not wall time)."""

    def wait(event, _timeout):
        return event.is_set()

    return wait


def test_a_silent_turn_on_selects_the_others_and_opens_on_a_late_buffer(tmp_path, quiet_turn_on):
    recorder, sides, signals = start(tmp_path, {"microphone": ("ok", 0.1), "system": ("ok", 0.2)})
    try:
        session = recorder.session
        turn_on(recorder, True, False)
        recorder._turn_on_wait = quiet_turn_on
        sides.plan["system"] = ("never", 0)  # The turned-on monitor is silent.
        state = turn_on(recorder, True, True)
        assert (state.me, state.others) == (True, True)
        at = session.manifest["events"][-1]["atMs"]
        assert session.manifest["events"][-2:] == [
            dict(atMs=at, type="selection", me=True, others=True, reason="user"),
            dict(atMs=at, type="othersSource", source="system", reason="user"),
        ]
        # Selected, armed, no track and no refusal.
        assert [t["kind"] for t in session.manifest["tracks"] if not t.get("ended")] == ["microphone"]
        assert "refusals" not in session.manifest and recorder._pending_sources["system"]["armed"]
        assert live(session) == []
        # The same selection is not reopened while it is armed.
        armed = recorder._pending_sources["system"]
        assert turn_on(recorder, True, True) == state and recorder._pending_sources["system"] is armed
        sides.fire("system")
        spin_until(lambda: recorder._track_ids.get("system", "system") != "system")
        identifier = recorder._track_ids["system"]
        spin_until(lambda: session.writers[identifier].frames == 480)
        track = next(t for t in session.manifest["tracks"] if t["id"] == identifier)
        assert track["startOffsetMs"] >= at and not track.get("ended")
        stop(recorder, signals)
    finally:
        close(recorder)
    assert saved(session)["state"] == "completed" and "refusals" not in saved(session)


def test_an_armed_turn_on_can_be_turned_off_again(tmp_path, quiet_turn_on):
    recorder, sides, signals = start(tmp_path, {"microphone": ("ok", 0.1), "system": ("ok", 0.2)})
    try:
        session = recorder.session
        turn_on(recorder, True, False)
        recorder._turn_on_wait = quiet_turn_on
        sides.plan["system"] = ("never", 0)
        turn_on(recorder, True, True)
        branch = recorder._pending_sources["system"]["branch"]
        state = turn_on(recorder, True, False)
        assert (state.me, state.others) == (True, False)
        assert session.manifest["events"][-1]["type"] == "othersSource"
        assert session.manifest["events"][-1]["source"] == "none"
        assert not recorder._pending_sources and branch.get_parent() is None
        assert [t["kind"] for t in session.manifest["tracks"]] == ["microphone", "system"]  # Only the first span.
        stop(recorder, signals)
    finally:
        close(recorder)
    assert saved(session)["state"] == "completed"


def test_an_armed_turn_on_refused_by_an_explicit_error_is_refused_at_its_time(tmp_path, quiet_turn_on):
    recorder, sides, signals = start(tmp_path, {"microphone": ("ok", 0.1), "system": ("fail", 0)})
    try:
        session = recorder.session
        recorder._turn_on_wait = quiet_turn_on
        sides.plan["system"] = ("never", 0)
        recorder._elapsed_ms = lambda _pipeline: 7000
        turn_on(recorder, True, True)  # Try again: armed, the start refusal stays.
        assert recorder._pending_sources["system"]["armed"]
        post_error(sides, "system", DENIED)
        spin_until(lambda: len(session.manifest["refusals"]) == 2)
        assert [(r["atMs"], r["code"]) for r in session.manifest["refusals"]] == [
            (0, "deviceUnavailable"),
            (7000, "permissionDenied"),
        ]
        assert recorder.refusals[-1]["atMs"] == 0  # The start's list is unchanged.
        assert not recorder._finishing
        stop(recorder, signals)
    finally:
        close(recorder)
    assert saved(session)["state"] == "completed"


# The browser path's system tap (BrowserCapture with others from computer sound).


class QuietTaps:
    """A tap factory: the microphone delivers at once, the system tap only when told."""

    def __init__(self):
        self.made = []

    def __call__(self, kind, origin, append, failed, *, level):
        taps = self

        class Tap:
            pipeline = None
            refusal_code = "deviceUnavailable"

            def __init__(self):
                self.kind, self.append, self.failed = kind, append, failed
                self.stopped = threading.Event()

            def start(self):
                if kind == "microphone":
                    self.append(struct.pack("<h", 1) * 480, 0)  # Its first buffer.

            def stop(self):
                self.stopped.set()

        tap = Tap()
        taps.made.append(tap)
        return tap

    def of(self, kind):
        return [tap for tap in self.made if tap.kind == kind]


def test_a_silent_system_tap_commits_armed_and_records_from_its_late_first_buffer(tmp_path):
    taps = QuietTaps()
    capture, result, changes = browser_start(tmp_path, taps, others_source="system")
    try:
        session = capture.host.session
        assert result["type"] == "started" and not capture.finished and "error" not in changes
        assert set(capture.native_taps) == {"microphone", "system"} and kinds_of(session) == ["microphone"]
        assert "refusals" not in session.manifest and not capture.refused_sides
        assert live(session) == []
        at = capture.elapsed_ms() + 10500
        (system,) = taps.of("system")
        system.append(struct.pack("<h", 7) * 480, at)
        assert kinds_of(session) == ["microphone", "system"]
        track = session.manifest["tracks"][1]
        assert track["startOffsetMs"] == pytest.approx(at) and track["frames"] == 480
        assert track["file"] == "track-system.wav" and "sources" in changes
    finally:
        capture.stop()
    assert session.manifest["state"] == "completed" and "refusals" not in session.manifest


def test_a_system_tap_silent_until_stop_leaves_no_track_and_no_refusal(tmp_path):
    taps = QuietTaps()
    capture, _result, changes = browser_start(tmp_path, taps, others_source="system")
    session = capture.host.session
    native_pcm(capture, "microphone")
    capture.stop()
    assert kinds_of(session) == ["microphone"] and "refusals" not in session.manifest
    assert session.manifest["state"] == "completed" and "error" not in changes
    assert taps.of("system")[0].stopped.is_set()


@pytest.mark.parametrize("code", ["permissionDenied", "deviceUnavailable"])
def test_an_armed_system_tap_refused_by_an_explicit_error_keeps_the_recording(tmp_path, code):
    taps = QuietTaps()
    capture, _result, changes = browser_start(tmp_path, taps, others_source="system")
    try:
        session = capture.host.session
        (system,) = taps.of("system")
        system.refusal_code = code
        system.failed("Fake monitor denied")
        assert session.manifest["refusals"] == [dict(atMs=0, side="others", code=code, message="Fake monitor denied")]
        assert "system" not in capture.native_taps and capture.refused_sides == {"system"}
        assert not capture.error and "error" not in changes and not capture.finished
        assert system.stopped.wait(5)  # Bound only: stopped off the tap's own thread.
        native_pcm(capture, "microphone")
    finally:
        capture.stop()
    assert session.manifest["state"] == "completed"


def test_a_silent_system_turn_on_is_armed_and_refused_only_at_its_time(tmp_path):
    taps = QuietTaps()
    capture, _result, _changes = browser_start(tmp_path, taps, others_source="system")
    try:
        session = capture.host.session
        capture.select(True, False)
        capture.select(True, True)
        turned_on = [e for e in session.manifest["events"] if e["type"] == "selection"][-1]["atMs"]
        assert kinds_of(session) == ["microphone"] and "system" in capture.native_taps
        taps.of("system")[-1].failed("Fake monitor went away")
        assert session.manifest["refusals"] == [
            dict(atMs=turned_on, side="others", code="deviceUnavailable", message="Fake monitor went away")
        ]
    finally:
        capture.stop()


# The real tap: no first buffer is not a failure for the system side.


class Devices(list):
    """The silent devices made; `deny` posts a denied access as each is made."""

    deny = False


@pytest.fixture
def silent_device(monkeypatch):
    """NativeTap's test source replaced by a silent appsrc, and a short window."""
    from cassini_talk import native_tap

    parse = Gst.parse_launch
    made = Devices()

    def launch(description):
        description = description.replace(
            description.split(" ! ", 1)[0],
            "appsrc name=device is-live=true format=time do-timestamp=true "
            'caps="audio/x-raw,format=S16LE,rate=48000,channels=1,layout=interleaved"',
        )
        pipeline = parse(description)
        made.append(pipeline)
        if made.deny:
            # Queued on the bus before the tap reads it: within its window.
            device = pipeline.get_by_name("device")
            error = GLib.Error.new_literal(Gst.ResourceError.quark(), "Access denied", int(DENIED))
            device.post_message(Gst.Message.new_error(device, error, "scripted"))
        return pipeline

    monkeypatch.setattr(native_tap.Gst, "parse_launch", launch)
    monkeypatch.setattr(native_tap, "OPEN_TIMEOUT_S", 0.2, raising=False)
    return made


def test_a_silent_system_tap_starts_armed_and_stops_without_waiting(silent_device):
    from cassini_talk.native_tap import NativeTap

    received, failures = [], []
    tap = NativeTap("system", 0, lambda data, *_a, **_k: received.append(data), failures.append, test=True)
    tap.start()  # Returns after its window: armed, not refused.
    assert not tap.heard.is_set() and tap.error is None
    device = silent_device[0].get_by_name("device")
    assert device.emit("push-buffer", Gst.Buffer.new_wrapped(bytes(960))) == Gst.FlowReturn.OK
    assert tap.heard.wait(5)  # Bound only: the late first buffer reaches append.
    assert len(received) == 1 and not failures
    tap.stop()


def test_a_never_heard_system_tap_stops_at_once(silent_device):
    from cassini_talk.native_tap import NativeTap

    tap = NativeTap("system", 0, lambda *_a, **_k: None, lambda _m: None, test=True)
    tap.start()
    # Like a Pulse read on a suspended monitor, this device never lets an EOS through.
    pad = silent_device[0].get_by_name("device").get_static_pad("src")
    pad.add_probe(
        Gst.PadProbeType.EVENT_DOWNSTREAM,
        lambda _pad, info: (
            Gst.PadProbeReturn.DROP if info.get_event().type == Gst.EventType.EOS else Gst.PadProbeReturn.OK
        ),
    )
    began = time.monotonic()
    tap.stop()  # No EOS to drain from a device that never delivered: no 10 s timeout.
    assert time.monotonic() - began < 5


@pytest.mark.parametrize("kind", ["microphone", "system"])
def test_a_silent_tap_window(silent_device, kind):
    from cassini_talk.native_tap import NativeTap

    tap = NativeTap(kind, 0, lambda *_a, **_k: None, lambda _m: None, test=True)
    if kind == "microphone":
        with pytest.raises(OSError, match="did not produce a sample"):
            tap.start()  # The microphone keeps its bound.
    else:
        tap.start()
        tap.stop()


def test_an_explicit_tap_error_carries_its_code(silent_device):
    from cassini_talk.native_tap import CaptureError, NativeTap

    silent_device.deny = True
    tap = NativeTap("system", 0, lambda *_a, **_k: None, lambda _m: None, test=True)
    with pytest.raises(CaptureError) as raised:
        tap.start()
    assert raised.value.refusal_code == "permissionDenied" and str(raised.value) == "Access denied"


def test_the_sound_check_reads_a_never_heard_others_side_as_silent():
    from cassini_talk.refusals import entry
    from cassini_talk.sound_check import REFUSED, SILENT, Outcome, judge_streams

    mic = entry(0, "me", "deviceUnavailable", message="No such entity")
    outcomes = judge_streams(None, [], [mic], {"system"})
    assert outcomes == dict(microphone=Outcome(REFUSED, mic), system=Outcome(SILENT))
    # Without that fact, a side that never opened is still refused.
    assert judge_streams(None, [], [mic])["system"].state == REFUSED


# Waiting for sound shows no refusal (D58.1): the recording page's lines read the
# live armed sides, which the folder cannot express (Try again writes no event).


def shown(recorder):
    """The recording page's lines for a native recording, as the window reads them."""
    manifest = recorder.session.manifest
    tracks = [(t["kind"], t.get("ended"), t.get("startOffsetMs")) for t in manifest["tracks"]]
    return R.live_lines(manifest.get("refusals"), tracks, manifest.get("events") or (), recorder.armed_sides())


def browser_shown(capture):
    """The same for a browser capture: its folder plus the armed sides of its status."""
    manifest = capture.host.session.manifest
    tracks = [(t["kind"], t.get("ended"), t.get("startOffsetMs")) for t in manifest["tracks"]]
    return R.live_lines(manifest.get("refusals"), tracks, manifest.get("events") or (), capture.snapshot()["armed"])


OTHERS_LINE = dict(side="others", id="errors.deviceUnavailable.others", caption=None)


def test_an_armed_side_covers_only_the_refusals_before_its_attempt():
    stored = [R.entry(0, "others", "deviceUnavailable")]
    assert R.live_lines(stored, []) == [OTHERS_LINE]
    assert R.live_lines(stored, [], (), {"system": 0}) == []
    assert R.live_lines(stored, [], (), {"system": 7000}) == []
    later = [R.entry(7000, "others", "permissionDenied")]
    assert R.live_lines(later, [], (), {"system": 3000}) == [OTHERS_LINE]
    # Not the others' kind, or unreadable: no cover.
    assert R.live_lines(stored, [], (), {"microphone": 9000}) == [OTHERS_LINE]
    assert R.live_lines(stored, [], (), {"system": "9000"}) == [OTHERS_LINE]
    assert R.live_lines(stored, [], (), [("system", 9000)]) == [OTHERS_LINE]
    # Computer sound selected and waiting records the others: the fallback line.
    browser = [R.entry(0, "browser", "startRejected", detail="noSources")]
    assert [line["id"] for line in R.live_lines(browser, [], (), {"system": 0})] == ["errors.othersFallback.line"]
    assert R.live_lines(browser, []) == []


def test_try_again_on_refused_others_that_stay_silent_shows_no_refusal(tmp_path, quiet_turn_on):
    recorder, sides, signals = start(tmp_path, {"microphone": ("ok", 0.1), "system": ("fail", 0)})
    try:
        session = recorder.session
        assert shown(recorder) == [OTHERS_LINE]
        recorder._turn_on_wait = quiet_turn_on
        sides.plan["system"] = ("never", 0)
        recorder._elapsed_ms = lambda _pipeline: 7000
        turn_on(recorder, True, True)  # Try again: silent, so armed.
        assert recorder.armed_sides() == {"system": 7000}
        assert shown(recorder) == []
        # A later explicit error is its own refusal, at the attempt's time.
        post_error(sides, "system", DENIED)
        spin_until(lambda: len(session.manifest.get("refusals", ())) == 2)
        spin_until(lambda: recorder.armed_sides() == {})
        assert shown(recorder) == [OTHERS_LINE]
        assert session.manifest["refusals"][-1]["atMs"] == 7000
        stop(recorder, signals)
    finally:
        close(recorder)


def test_refused_others_turned_off_then_on_and_silent_show_no_refusal(tmp_path, quiet_turn_on):
    recorder, sides, signals = start(tmp_path, {"microphone": ("ok", 0.1), "system": ("fail", 0)})
    try:
        recorder._turn_on_wait = quiet_turn_on
        recorder._elapsed_ms = lambda _pipeline: 4000
        turn_on(recorder, True, False)
        assert shown(recorder) == []
        recorder._elapsed_ms = lambda _pipeline: 8000
        sides.plan["system"] = ("never", 0)
        turn_on(recorder, True, True)
        assert recorder.armed_sides() == {"system": 8000} and shown(recorder) == []
        stop(recorder, signals)
    finally:
        close(recorder)


def test_the_page_reads_armed_sides_while_stop_holds_the_recorder_lock(tmp_path):
    # Boards' quit stop-finishing: the GTK main loop asked for armed_sides while
    # Stop held the recorder lock, and the page stalled until the session was
    # left recording.
    recorder, sides, signals = start(tmp_path, QUIET)
    try:
        read = threading.Event()
        found = []
        with recorder._lock:
            worker = threading.Thread(target=lambda: (found.append(recorder.armed_sides()), read.set()), daemon=True)
            worker.start()
            assert read.wait(5), "armed_sides waited on the recorder lock"
        assert found == [{"system": 0}]
        stop(recorder, signals)
    finally:
        close(recorder)


def test_a_silent_start_is_armed_at_zero_and_shows_no_line(tmp_path):
    recorder, sides, signals = start(tmp_path, QUIET)
    try:
        assert recorder.armed_sides() == {"system": 0} and shown(recorder) == []
        sides.now = BOUND + 5
        sides.fire("system")
        spin_until(lambda: "system" in recorder._track_ids)
        assert recorder.armed_sides() == {}
        stop(recorder, signals)
    finally:
        close(recorder)


def test_browser_try_again_on_a_refused_system_tap_that_stays_silent_shows_no_refusal(tmp_path):
    taps = QuietTaps()
    capture, _result, _changes = browser_start(tmp_path, taps, others_source="system")
    try:
        session = capture.host.session
        assert capture.snapshot()["armed"] == {"system": 0}
        (system,) = taps.of("system")
        system.failed("Fake monitor went away")  # An explicit error: refused at 0.
        assert capture.snapshot()["armed"] == {} and browser_shown(capture) == [OTHERS_LINE]
        capture.select(True, True, retry=True)  # Try again: silent, so armed.
        armed = capture.snapshot()["armed"]
        assert set(armed) == {"system"} and armed["system"] > 0 and browser_shown(capture) == []
        # Its first buffer gives it a track; it is no longer armed.
        taps.of("system")[-1].append(struct.pack("<h", 7) * 480, armed["system"] + 10)
        assert kinds_of(session) == ["microphone", "system"] and capture.snapshot()["armed"] == {}
        assert browser_shown(capture) == []
    finally:
        capture.stop()


def test_the_window_reads_a_browser_capture_s_armed_sides_from_its_status(tmp_path):
    from types import SimpleNamespace
    from uuid import uuid4

    try:
        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
    except ValueError:
        pytest.skip("Adwaita is unavailable")
    from cassini_talk.library_window import LibraryWindow

    identifier = str(uuid4())
    directory = tmp_path / f"Recording-{identifier}"
    status = dict(path=str(directory), recording=True, armed={"system": 1200})
    app = SimpleNamespace(_browser_snapshot=dict(statuses=[dict(path=str(tmp_path / "Recording-x"), armed={})]))
    window = SimpleNamespace(_browser_snapshot=dict(statuses=[status]), get_application=lambda: app)
    meeting = SimpleNamespace(id=identifier, directory=directory)
    assert LibraryWindow._armed_sides(window, meeting) == {"system": 1200}
    window._browser_snapshot = {}
    assert LibraryWindow._armed_sides(window, meeting) is None
    app._browser_snapshot = dict(statuses=[status])
    assert LibraryWindow._armed_sides(window, meeting) == {"system": 1200}
    assert LibraryWindow._armed_sides(window, SimpleNamespace(id=str(uuid4()), directory=directory)) is None


# The browser path ends like the native one when no side ever opened (D58 point 2).


class RefusingMicrophone(QuietTaps):
    """The microphone tap reports an explicit error at start; the system tap is quiet."""

    def __call__(self, kind, origin, append, failed, *, level):
        tap = super().__call__(kind, origin, append, failed, level=level)
        if kind == "microphone":

            def start():
                raise OSError("No such entity")

            tap.start = start
        return tap


def test_browser_refused_microphone_and_others_silent_until_stop_leave_no_folder(tmp_path):
    taps = RefusingMicrophone()
    capture, result, changes = browser_start(tmp_path, taps, others_source="system")
    session = capture.host.session
    try:
        assert result["type"] == "started" and capture.snapshot()["armed"] == {"system": 0}
        assert [(r["side"], r["code"]) for r in session.manifest["refusals"]] == [("me", "deviceUnavailable")]
        assert [line["side"] for line in browser_shown(capture)] == ["me"]
    finally:
        stopped = capture.stop()
    # Nothing ever opened: no folder, never the "empty" problem.
    assert stopped == dict(type="stopped", path=None) and capture.discarded and capture.nothing_recorded
    assert not session.directory.exists() and "error" not in changes


def test_browser_refused_microphone_and_a_late_others_error_end_with_no_folder(tmp_path):
    taps = RefusingMicrophone()
    capture, _result, changes = browser_start(tmp_path, taps, others_source="system")
    session = capture.host.session
    try:
        (system,) = taps.of("system")
        system.refusal_code = "permissionDenied"
        system.failed("Fake monitor denied")
        assert [(r["side"], r["code"]) for r in session.manifest["refusals"]] == [
            ("me", "deviceUnavailable"),
            ("others", "permissionDenied"),
        ]
        # Nothing is left that could open: the capture asks to end (as the service does on "error").
        assert "error" in changes and capture.error == "Fake monitor denied"
    finally:
        stopped = capture.stop(capture.error)
    assert stopped == dict(type="stopped", path=None) and capture.discarded and not session.directory.exists()


def test_browser_silent_others_with_a_recorded_microphone_keep_the_folder(tmp_path):
    taps = QuietTaps()
    capture, _result, _changes = browser_start(tmp_path, taps, others_source="system")
    session = capture.host.session
    native_pcm(capture, "microphone")
    assert capture.stop()["path"] == str(session.directory) and not capture.discarded
    assert session.manifest["state"] == "completed" and session.manifest.get("problem") is None


def test_browser_an_opened_microphone_keeps_the_folder_before_its_first_recorded_frame(tmp_path):
    """Opened is a committed track (as natively), not a frame count: no discard."""
    taps = QuietTaps()
    capture, _result, _changes = browser_start(tmp_path, taps, others_source="system")
    session = capture.host.session
    assert kinds_of(session) == ["microphone"] and session.manifest["tracks"][0]["frames"] == 0
    assert capture.stop()["path"] == str(session.directory) and not capture.discarded
    assert session.directory.exists()
