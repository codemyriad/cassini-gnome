# SPDX-License-Identifier: GPL-3.0-or-later
"""D58 shared refusal vectors (conformance/refusals) through GNOME's own code.

GNOME is the `fallback` profile. CI can never produce a real device or TCC
denial, so a case's refusals are injected where a side fails to open: the
side's open really fails (a state change, a refused tap Start, an ok:false or
unanswered browser Start), and only the code and raw message it reports are
taken from the vector, because GNOME itself knows only deviceUnavailable.
Everything else is real: the start paths, the session writer, the no-folder
rule, captureRecord, the start banner and the Details reader.
"""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

gi = pytest.importorskip("gi")
try:
    gi.require_version("Gst", "1.0")
except ValueError:
    pytest.skip("GStreamer typelib is unavailable", allow_module_level=True)
from gi.repository import Gst  # noqa: E402
from test_native_sides import FRAME, Sides  # noqa: E402
from test_recorder import spin_until  # noqa: E402

from cassini_talk import refusals  # noqa: E402
from cassini_talk.native_recorder import NativeRecorder  # noqa: E402

Gst.init(None)
VECTORS = Path(__file__).resolve().parents[1] / "browser-protocol/conformance/refusals"
WRITER = json.loads((VECTORS / "writer.json").read_text())
READERS = json.loads((VECTORS / "readers.json").read_text())
KINDS = {"me": "microphone", "others": "system"}
SIDE_OF = {"microphone": "me", "system": "others", "remote": "browser"}
PROFILE = "fallback"


def gnome_cases():
    return [case for case in WRITER["cases"] if case.get("profile", PROFILE) == PROFILE]


def start_refusals(case):
    return [s for s in case["steps"] if s["op"] == "refuse" and s["atMs"] == 0 and not turned_on(case, s)]


def turned_on(case, step):
    steps = case["steps"]
    index = steps.index(step)
    back = index - 1
    while back >= 0 and steps[back]["op"] == "refuse" and steps[back]["atMs"] == step["atMs"]:
        back -= 1
    return back >= 0 and steps[back]["op"] == "select" and steps[back]["atMs"] == step["atMs"]


def observed(saved, *, selected_others):
    """The keys a writer case checks, from a saved session.json."""
    events = saved.get("events", [])
    sources = [e for e in events if e["type"] == "othersSource"]
    opened = {SIDE_OF[t["kind"]] for t in saved["tracks"] if t["kind"] in SIDE_OF}
    return dict(
        folder=True,
        opened=[side for side in ("me", "others", "browser") if side in opened],
        selection=[
            dict(atMs=e["atMs"], me=e["me"], others=e["others"], reason=e["reason"])
            for e in events
            if e["type"] == "selection"
        ],
        othersFrom=sources[-1]["source"] if sources and selected_others else None,
        state=saved["state"],
        problemKind=saved.get("problemKind"),
        refusals=saved.get("refusals"),
        banner=None,
    )


def no_folder(banner):
    return dict(
        folder=False,
        opened=[],
        selection=None,
        othersFrom=None,
        state=None,
        problemKind=None,
        refusals=None,
        banner=dict(id=banner["id"], refusals=banner["refusals"]),
    )


def check(case, actual, directory=None):
    for key, value in case["expected"].items():
        assert actual[key] == value, (case["name"], key, actual[key], value)
    if directory is not None:
        # Rule 7 (each app): captureRecord.refusals copies the session list, also
        # without captureOutcome (with sources: []).
        from cassini_talk.capture_report import record_capture
        from cassini_talk.meeting_store import read_metadata

        saved = json.loads((directory / "session.json").read_text())
        record_capture(directory, saved)
        record = read_metadata(directory).get("recordingDetails", {}).get("captureRecord")
        if "refusals" in saved:
            assert record["refusals"] == saved["refusals"], case["name"]
            if "captureOutcome" not in saved:
                assert record["sources"] == []
        else:
            assert record is None or "refusals" not in record


# The native start path: request others = system (a plain native Record).


@pytest.mark.parametrize(
    "case", [c for c in gnome_cases() if c["request"]["others"] == "system"], ids=lambda c: c["name"]
)
def test_native_writer_cases(tmp_path, case):
    recorder = NativeRecorder()
    now = dict(ms=0)
    injected = {}  # kind -> the refuse step its next failed open reports
    for step in start_refusals(case):
        injected[KINDS[step["side"]]] = step
    plan = {kind: ("fail", 0) if kind in injected else ("ok", 0) for kind in ("microphone", "system")}
    sides = Sides(recorder, plan)
    recorder._elapsed_ms = lambda _pipeline: now["ms"]
    recorder._classify = lambda kind, _attempt: (injected[kind]["code"], injected[kind].get("message"))
    signals = []
    recorder.connect("started", lambda *_: signals.append("started"))
    recorder.connect("finished", lambda _r, path: signals.append(("finished", path)))
    recorder.start(tmp_path, test=True)
    try:
        spin_until(lambda: not recorder.starting and (signals or recorder._finishing))
        if not recorder.discarded and not recorder._finishing:
            replay_native(case, recorder, sides, now, injected)
        if recorder.pipeline is not None and not recorder._finishing:
            # Each opened side records until stop: its start branch delivers PCM.
            fed = [kind for kind, identifier in recorder._track_ids.items() if identifier == kind]
            for kind in fed:
                sides.feed(kind, 3)
            spin_until(lambda: all(recorder.session.writers[kind].frames for kind in fed))
            recorder.stop()
        spin_until(lambda: any(isinstance(s, tuple) for s in signals))
    finally:
        if recorder.pipeline is not None:
            recorder.stop()
            spin_until(lambda: recorder.pipeline is None and not recorder._finishing)
        recorder._executor.shutdown(wait=True)
    if recorder.discarded:
        assert not recorder.session.directory.exists() and not list(tmp_path.iterdir())
        check(case, no_folder(refusals.banner(recorder.refusals)))
        return
    saved = json.loads((recorder.session.directory / "session.json").read_text())
    selected = [e for e in saved["events"] if e["type"] == "selection"][-1]["others"]
    check(case, observed(saved, selected_others=selected), recorder.session.directory)


def replay_native(case, recorder, sides, now, injected):
    from cassini_talk.session_events import source_state

    steps = case["steps"]
    for index, step in enumerate(steps):
        if step["op"] != "select" or recorder._finishing:
            if step["op"] == "fail" and not recorder._finishing:
                kind = KINDS[step["side"]]
                source = sides.sources[kind]
                source.post_message(Gst.Message.new_error(source, gi.repository.GLib.Error("Fake failure"), "P10"))
                spin_until(lambda: recorder._finishing or recorder.pipeline is None)
            continue
        now["ms"] = step["atMs"]
        refused = [
            s for s in steps[index + 1 :] if s["op"] == "refuse" and s["atMs"] == step["atMs"] and turned_on(case, s)
        ]
        current = source_state(recorder.session.manifest["events"])
        me, others = step.get("me", current.me), step.get("others", current.others)
        for kind in ("microphone", "system"):
            sides.plan.pop(kind, None)
        for refusal in refused:
            kind = KINDS[refusal["side"]]
            injected[kind] = refusal
            sides.plan[kind] = ("fail", 0)  # This turn-on's device refuses.
        future = recorder.select(me, others)
        spin_until(lambda future=future: future.done() and not recorder._changing_sources)
        if refused:
            with pytest.raises(OSError):
                future.result()
        else:
            future.result()


# The browser start path: request others = browser, through the broker.


class Injected(OSError):
    def __init__(self, step):
        super().__init__(step.get("message") or "injected")
        self.step = step


class Taps:
    refused = {}

    def __init__(self, kind, origin, append, failed, *, level):
        self.kind, self.append = kind, append
        self.pipeline = None

    def start(self):
        step = Taps.refused.get(self.kind)
        if step is not None:
            raise Injected(step)
        # A side opens on its first buffer (D58.1: a silent system tap stays
        # armed, without a track); this readiness PCM precedes the commit.
        self.append(FRAME, 0)

    def stop(self):
        pass


@pytest.fixture
def broker(tmp_path, monkeypatch):
    from broker_fixture import stop_broker

    from cassini_talk.browser_capture import BrowserCapture
    from cassini_talk.browser_service import BrowserService

    classify = BrowserCapture._classify

    def injected(kind, error):
        if isinstance(error, Injected):
            return error.step["code"], error.step.get("message")
        return classify(kind, error)

    monkeypatch.setattr(BrowserCapture, "_classify", staticmethod(injected))
    Taps.refused = {}
    service = BrowserService(
        tmp_path / "config.json",
        capture_parent=tmp_path / "library",
        tap_factory=Taps,
        notifier=lambda _title, _stop: None,
        command_timeout=1,
    )
    path = tmp_path / "broker/browser.sock"
    ready = threading.Event()
    worker = threading.Thread(target=service.serve, args=(path, ready), daemon=True)
    worker.start()
    assert ready.wait(2)
    yield service, path, tmp_path / "library"
    stop_broker(service, worker, path)


@pytest.mark.parametrize(
    "case", [c for c in gnome_cases() if c["request"]["others"] == "browser"], ids=lambda c: c["name"]
)
def test_browser_writer_cases(broker, case):
    from test_browser_sessions import Extension

    from cassini_talk.browser_service import request, send_packet

    service, path, library = broker
    browser = next(s for s in start_refusals(case) if s["side"] == "browser")
    Taps.refused = {KINDS[s["side"]]: s for s in start_refusals(case) if s["side"] != "browser"}
    peer = Extension(path, (3,))
    try:
        with ThreadPoolExecutor(1) as pool:
            pending = pool.submit(request, "start-all", path=path)
            command = peer.receive()
            assert command["action"] == "start"
            if browser["code"] == "startRejected":
                result = dict(type="result", requestId=command["requestId"], ok=False, status=dict(tabId=3))
                if "message" in browser:
                    result["error"] = browser["message"]
                if "detail" in browser:
                    result["code"] = browser["detail"]
                send_packet(peer.stream, result)
            # startFailed/timeout: the extension never answers this Start.
            snapshot = pending.result(10)
        active = [s for s in snapshot["statuses"] if s.get("recording") and s.get("path")]
        if not active:
            assert snapshot["failures"] and not list(library.glob("Recording-*"))
            check(case, no_folder(refusals.banner(snapshot["failures"][0]["refusals"])))
            return
        capture = service.captures[active[0]["captureId"]]
        for identifier, tap in capture.native_taps.values():
            track = next(t for t in capture.host.session.manifest["tracks"] if t["id"] == identifier)
            tap.append(FRAME, track["startOffsetMs"])  # Each opened side records until stop.
        capture.stop(reason="user")
        saved = json.loads((capture.directory / "session.json").read_text())
        check(case, observed(saved, selected_others=True), capture.directory)
    finally:
        peer.close()


# Wire: an ok:false Start result becomes the browser refusal it records.


@pytest.mark.parametrize("case", WRITER["wire"], ids=lambda c: c["name"])
def test_wire_cases(case):
    from cassini_talk.browser_service import StartRejected

    result = case["result"]
    refusal = StartRejected(str(result.get("error", ""))[:500], result).refusal
    assert refusal.pop("atMs") == 0
    assert refusal == case["expected"]


# Readers: Audio sources lines and Copy details, through read_details.


@pytest.fixture
def meeting(tmp_path):
    from details_fixture import build_library

    from cassini_talk.library import LibraryScanner

    paths = build_library(tmp_path)
    scanned = {m.directory: m for m in LibraryScanner(tmp_path).scan().meetings}
    return scanned[paths["microphone"]]


def not_selected_row(side):
    kind = "microphone" if side == "me" else "remote"
    return dict(
        sourceTrackId="not-selected-" + side,
        kind=kind,
        label="Guest",
        captureOutcome="neverAudio",
        captureExpected=False,
        audioMs=0,
        trackIds=[],
    )


@pytest.mark.parametrize("where", ["session", "captureRecord"])
@pytest.mark.parametrize("case", READERS["readers"], ids=lambda c: c["name"])
def test_reader_cases(meeting, case, where):
    from dataclasses import replace

    from cassini_talk.capture_details import read_details

    session_path = meeting.directory / "session.json"
    session = json.loads(session_path.read_text())
    rows = [*case.get("sources", ()), *(not_selected_row(side) for side in case.get("notSelected", ()))]
    record = dict(version=1, sources=rows, tracks=[], events=session.get("events", []))
    if where == "session":
        session["refusals"] = case["stored"]
        session_path.write_text(json.dumps(session))
    else:
        record["refusals"] = case["stored"]
        session_path.unlink()  # A published file: only its captureRecord remains.
    report = read_details(
        replace(meeting, metadata=dict(meeting.metadata, recordingDetails=dict(captureRecord=record)))
    )
    lines = [
        dict(id=line.id, source=line.source) if line.source else dict(id=line.id)
        for line in report.audio
        if line.source or line.id == "details.sources.empty"
    ]
    assert lines == case["expectedLines"], case["name"]
    copied = refusals.read(case["stored"])
    assert list(report.refusals) == copied
    if "expectedCopy" in case:
        assert list(report.refusals) == case["expectedCopy"], case["name"]
    text = report.plain_text()
    for item in copied:
        assert "  " + json.dumps(item, ensure_ascii=False, separators=(",", ":")) in text


def test_every_gnome_case_is_replayed():
    names = {c["name"] for c in gnome_cases()}
    assert names == {c["name"] for c in WRITER["cases"] if c.get("profile") != "noFallback"}
    assert {c["request"]["others"] for c in gnome_cases()} == {"system", "browser"}
