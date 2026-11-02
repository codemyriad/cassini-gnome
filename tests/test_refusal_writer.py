# SPDX-License-Identifier: GPL-3.0-or-later
"""D58 (R79) refusals on GNOME: writer rules, no folder, captureRecord, browser
start failures, a refused resume, the hello version and the live copy."""

import json
import logging
from pathlib import Path

import pytest
from test_browser_service import service  # noqa: F401
from test_resume_capture_host import broker  # noqa: F401
from test_voice_application import application_class  # noqa: F401

from cassini_talk import refusals
from cassini_talk.recording_session import RecordingSession


def test_an_entry_has_only_the_fields_given_in_protocol_order():
    item = refusals.entry(1500.5, "me", "deviceUnavailable", detail="busy", message="x")
    assert list(item) == ["atMs", "side", "code", "detail", "message"]
    assert refusals.entry(0, "me", "deviceUnavailable") == dict(atMs=0, side="me", code="deviceUnavailable")
    # An empty message is omitted; a detail must match as a whole string.
    assert "message" not in refusals.entry(0, "me", "deviceUnavailable", message="")
    for detail in ("busy\n", "not a code", "a" * 33, 7):
        assert "detail" not in refusals.entry(0, "browser", "startRejected", detail=detail)
    # Cut at 200 code points: an emoji counts once, a combining mark on its own.
    message = "a" * 198 + "\U0001f600éz"
    assert refusals.entry(0, "me", "deviceUnavailable", message=message)["message"] == "a" * 198 + "\U0001f600e"


def test_the_cap_keeps_the_first_fifteen_and_the_latest():
    manifest = {}
    for index in range(20):
        refusals.append(manifest, refusals.entry(index, "me", "deviceUnavailable"))
    assert [item["atMs"] for item in manifest["refusals"]] == [*range(15), 19]


def test_a_refusal_is_checkpointed_and_changes_nothing_else(tmp_path):
    session = RecordingSession(tmp_path, source="gnome")
    with session.change([dict(atMs=0, type="selection", me=True, others=True, reason="start")]):
        pass
    before = json.loads((session.directory / "session.json").read_text())
    session.refuse(refusals.entry(0, "others", "deviceUnavailable", message="pulse"))
    after = json.loads((session.directory / "session.json").read_text())
    assert after.pop("refusals") == [dict(atMs=0, side="others", code="deviceUnavailable", message="pulse")]
    assert after == before
    session.add_track("microphone", "Me", "microphone", 48000, 1, filename="track-microphone.wav")
    session.append("microphone", bytes(960), 0)
    session.finish()
    saved = json.loads((session.directory / "session.json").read_text())
    assert saved["state"] == "completed" and "problem" not in saved and "problemKind" not in saved
    assert saved["events"][-1]["reason"] == "user"


def started(tmp_path):
    session = RecordingSession(tmp_path, source="gnome")
    with session.change([dict(atMs=0, type="selection", me=True, others=True, reason="start")]):
        pass
    return session


def test_nothing_opened_removes_the_unpublished_folder(tmp_path):
    session = started(tmp_path)
    assert session.discard() and session.finished
    assert not session.directory.exists() and not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "typed",
    [dict(title="Weekly sync", titleSource="user"), dict(notes=[dict(atMs=0, text="Agenda")])],
    ids=["title", "notes"],
)
def test_a_typed_title_or_notes_keep_the_folder(tmp_path, typed):
    from cassini_talk.meeting_store import update_metadata

    session = started(tmp_path)
    update_metadata(session.directory, **typed)
    assert not session.discard() and not session.finished
    assert (session.directory / "session.json").exists()
    session.finish("Could not start", reason="error")
    assert json.loads((session.directory / "session.json").read_text())["state"] == "interrupted"


def test_an_automatic_title_does_not_keep_the_folder(tmp_path):
    from cassini_talk.meeting_store import update_metadata

    session = started(tmp_path)
    update_metadata(session.directory, title="Meet", titleSource="meeting-window")
    assert session.discard() and not session.directory.exists()


def test_an_unknown_file_keeps_the_folder(tmp_path):
    session = started(tmp_path)
    (session.directory / "keep.bin").write_bytes(b"x")
    assert not session.discard() and (session.directory / "keep.bin").exists()


def test_capture_record_copies_refusals_even_without_capture_outcome():
    from cassini_talk.capture_report import from_session

    stored = [dict(atMs=0, side="me", code="deviceUnavailable", future=True)]
    record = from_session(dict(tracks=[], events=[], refusals=stored))
    assert record["sources"] == [] and record["refusals"] == stored and record["refusals"] is not stored
    assert from_session(dict(tracks=[], events=[])) is None
    with_outcome = from_session(dict(tracks=[], events=[], captureOutcome=dict(sources=[]), refusals=stored))
    assert with_outcome["refusals"] == stored
    # A non-list member is copied as stored; readers ignore it.
    assert from_session(dict(tracks=[], refusals="bad"))["refusals"] == "bad"


@pytest.mark.parametrize(
    "stored",
    [
        None,
        "not a list",
        [{"atMs": 0, "side": "browser", "code": "startRejected", "detail": None, "message": None}],
        [{"atMs": -1, "side": "a b", "code": "x", "message": "m" * 300}],
        [{"atMs": 0, "side": "me", "code": "deviceUnavailable"}] * 17,
    ],
    ids=["null", "non-list", "null-fields", "bad-entry", "over-cap"],
)
def test_odd_capture_record_refusals_never_invalidate_meeting_json(stored):
    """R81: captureRecord.refusals is documentation-only; readers filter it."""
    from cassini_talk import library_schema

    node = library_schema.SCHEMA["properties"]["recordingDetails"]["properties"]["captureRecord"]
    assert library_schema.valid(dict(version=1, sources=[], refusals=stored), node)


def test_captured_source_rows_stay_authoritative_next_to_refusals():
    from cassini_talk.source_report import sources

    row = dict(
        sourceTrackId="guest",
        kind="remote",
        label="Guest",
        captureOutcome="captured",
        audioMs=1000,
        trackIds=[],
    )
    metadata = dict(recordingDetails=dict(captureRecord=dict(version=1, sources=[row], tracks=[], events=[])))
    stored = [dict(atMs=0, side="me", code="deviceUnavailable")]
    assert [fact.identity for fact in sources(dict(tracks=[], refusals=stored), metadata, live=False)] == ["guest"]


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (TimeoutError("The browser did not finish the recording command"), "timeout"),
        (ValueError("The browser disconnected"), "disconnected"),
        (ValueError("A recording command is still being processed"), "busy"),
        (ValueError("This meeting is already being recorded"), "busy"),
        (ValueError("This meeting is no longer available"), "gone"),
        (OSError("No route"), None),
    ],
)
def test_start_failed_details_are_banner_only_and_carry_no_text(error, expected):
    from cassini_talk.browser_service import start_refusals

    (item,) = start_refusals(error)
    assert item == refusals.entry(0, "browser", "startFailed", detail=expected)


def test_a_cancelled_start_is_not_a_refusal():
    from cassini_talk.browser_service import StartRefused, start_refusals

    assert start_refusals(ValueError("Recording start was cancelled")) == []
    held = [refusals.entry(0, "browser", "startRejected", detail="busy")]
    assert start_refusals(StartRefused("busy", held)) == held


def test_banner_captions_follow_the_first_refusal():
    def caption(detail):
        return refusals.banner([refusals.entry(0, "browser", "startFailed", detail=detail)])["caption"]

    assert caption("busy") == "errors.refusal.busy"
    assert caption("noSources") == "errors.refusal.noSources"
    assert caption("timeout") == caption("disconnected") == "errors.refusal.noAnswer"
    assert caption("pageUnreachable") is None and caption(None) is None
    banner = refusals.banner([])
    assert (banner["id"], banner["caption"], banner["action"]) == (
        "errors.recordFailed.line",
        None,
        "common.tryAgain",
    )


def test_live_lines_show_each_side_that_records_nothing_now():
    stored = [
        refusals.entry(0, "browser", "startRejected", detail="noSources"),
        refusals.entry(0, "me", "permissionDenied"),
        refusals.entry(5000, "others", "deviceUnavailable"),
    ]
    system = [("system", None)]
    # The fallback line only while computer sound really records the others.
    lines = refusals.live_lines(stored[:2], system)
    assert [(line["side"], line["id"], line["caption"]) for line in lines] == [
        ("browser", "errors.othersFallback.line", "errors.refusal.noSources"),
        ("me", "errors.micPermission.line", None),
    ]
    # Computer sound refused too: no claim that it records, its own line instead.
    lines = refusals.live_lines(stored, [("system", True)])
    assert [(line["side"], line["id"]) for line in lines] == [
        ("me", "errors.micPermission.line"),
        ("others", "errors.deviceUnavailable.others"),
    ]
    # A side that records now shows nothing.
    assert [line["side"] for line in refusals.live_lines(stored, [("microphone", None), ("remote", False)])] == [
        "others"
    ]
    # A side turned off after its refusal shows nothing; a refused turn-on does.
    off = [dict(type="selection", atMs=7000, me=False, others=True, reason="user")]
    assert [line["side"] for line in refusals.live_lines(stored, system, off)] == ["browser"]
    turned_on = [refusals.entry(9000, "me", "deviceUnavailable")]
    assert [line["id"] for line in refusals.live_lines(turned_on, [], off)] == ["errors.deviceUnavailable.me"]
    # Unreadable entries are skipped, not shown.
    assert refusals.live_lines([7, dict(atMs=-1, side="me", code="x")], []) == []


def test_every_refusal_line_is_published_in_six_languages():
    """The per-code copy test: each (side, code) and caption maps to catalog copy."""
    from cassini_talk.strings import text

    catalog = json.loads(Path(refusals.__file__).with_name("strings.json").read_text(encoding="utf-8"))
    entries = {entry["id"]: entry for entry in catalog["entries"]}
    used = set()
    for side in refusals.SOURCES:
        for code in ("permissionDenied", "deviceUnavailable", "startRejected", "startFailed", "quotaExceeded"):
            key, source = refusals.line(dict(atMs=0, side=side, code=code))
            assert key == (
                "details.sources.refused." + code
                if code in ("permissionDenied", "deviceUnavailable", "startRejected")
                else "details.sources.failed"
            )
            assert "{source}" in entries[key]["en"] and source == refusals.SOURCES[side]
            used |= {key, source}
            for live in refusals.live_lines([dict(atMs=0, side=side, code=code)], []):
                used.add(live["id"])
    used |= set(refusals.CAPTIONS.values()) | {"errors.recordFailed.line", "common.tryAgain"}
    assert used >= {"errors.deviceUnavailable.me", "errors.deviceUnavailable.others", "errors.micPermission.line"}
    for key in used:
        assert set(entries[key]["l10n"]) >= {"it", "fr", "es", "pt-BR", "pt-PT"}, key
        assert "gnome" in entries[key]["platforms"], key
        assert text(key, source="X") if "{source}" in entries[key]["en"] else text(key)


# The browser host: a refused resume, and the hello version.


def test_refused_resume_is_start_rejected_in_the_same_folder(broker):  # noqa: F811
    from test_capture_record import declare, pcm
    from test_resume_capture_host import ACK, lose, proxy, resume, started

    client, capture = started(broker)
    assert client.handle(declare("guest", "doc-1:source:1")) == ACK
    assert client.handle(pcm("guest", 480)) == ACK
    lose(broker, client, capture)
    events = json.dumps(capture.host.session.manifest["events"])
    folder = capture.directory
    capture.mark_resume_ask("ask-1")  # R82 1b: refusals are recorded for an ask.
    attempt = proxy(broker)
    try:
        reply = attempt.handle(resume(capture, timelineOriginMs=capture.origin_ms + 1))
        assert reply["type"] == "error" and reply["scope"] == "command"
        saved = json.loads((folder / "session.json").read_text())
        (item,) = saved["refusals"]
        assert (item["side"], item["code"], item["detail"]) == ("browser", "startRejected", "resumeRefused")
        assert item["message"] == "Target is no longer active." and 0 < item["atMs"] <= refusals.MAX_MS
        # A refusal changes nothing else: no event, state or source.
        assert json.dumps(capture.host.session.manifest["events"]) == events
        assert not capture.finished and capture.browser_lost and capture.directory == folder
    finally:
        attempt.close()
        capture.stop()


def test_a_refused_resume_of_a_finished_recording_writes_nothing(broker):  # noqa: F811
    from test_capture_record import declare, pcm
    from test_resume_capture_host import ACK, lose, proxy, resume, started

    client, capture = started(broker)
    assert client.handle(declare("guest", "doc-1:source:1")) == ACK
    assert client.handle(pcm("guest", 480)) == ACK
    lose(broker, client, capture)
    capture.stop()
    before = (capture.directory / "session.json").read_bytes()
    attempt = proxy(broker)
    try:
        assert attempt.handle(resume(capture))["type"] == "error"
    finally:
        attempt.close()
    assert (capture.directory / "session.json").read_bytes() == before


def hello(path, version):
    from cassini_talk.browser_service import connect, send_packet

    connection = connect(path, timeout=2)
    stream = connection.makefile("rwb")
    send_packet(stream, dict(role="extension"))
    send_packet(stream, dict(type="hello", version=version, capabilities=[]))
    return connection, stream


@pytest.mark.parametrize("version", [2, 0, "1", None])
def test_a_newer_hello_is_visible_as_app_too_old(broker, caplog, version):  # noqa: F811
    from test_browser_service import wait_for

    from cassini_talk.browser_connection import BrowserConnection
    from cassini_talk.browser_service import request

    caplog.set_level(logging.WARNING, logger="cassini_talk.browser_service")
    connection, stream = hello(broker.path, version)
    try:
        # The broker closes the connection either way.
        connection.settimeout(5)
        assert stream.read(1) == b""
        wait_for(
            lambda: any("hello version" in r.getMessage() or "update Cassini" in r.getMessage() for r in caplog.records)
        )
        snapshot = request(path=broker.path)
        assert snapshot["appTooOld"] is (version == 2)
        controller = BrowserConnection(
            lambda: None, available=False, later=lambda *_: 0, post=lambda *_: 0, cancel=lambda *_: None
        )
        controller.observe(snapshot)
        assert (controller.snapshot()["phase"] == "appTooOld") is (version == 2)
    finally:
        stream.close()
        connection.close()
    if version == 2:
        # A compatible companion clears it.
        from test_browser_sessions import Extension

        peer = Extension(broker.path, (3,))
        try:
            assert request(path=broker.path)["appTooOld"] is False
        finally:
            peer.close()


def test_a_gstreamer_refusal_message_is_its_domain_code_and_debug_string():
    gi = pytest.importorskip("gi")
    gi.require_version("Gst", "1.0")
    from gi.repository import GLib, Gst

    Gst.init(None)
    error = GLib.Error.new_literal(Gst.ResourceError.quark(), "Translated text", int(Gst.ResourceError.OPEN_READ))
    expected = f"gst-resource-error-quark:{int(Gst.ResourceError.OPEN_READ)}: pulsesrc.c(1088): gst_pulsesrc_open"
    assert refusals.gst_message(error, "pulsesrc.c(1088): gst_pulsesrc_open") == expected
    assert refusals.gst_message(error, None) == f"gst-resource-error-quark:{int(Gst.ResourceError.OPEN_READ)}"


def test_a_fallback_whose_device_fails_is_not_a_turn_on_refusal(tmp_path):
    import time

    from test_native_sides import Taps

    from cassini_talk.browser_capture import BrowserCapture

    capture = BrowserCapture(lambda *_: None, parent=tmp_path, tap_factory=Taps(refuse={"system": 1}))
    capture.start(dict(type="start", captureId="lost", timelineOriginMs=int(time.time() * 1000), microphone=False))
    try:
        with pytest.raises(OSError, match="Fake system refused"):
            capture.fallback()
        assert "refusals" not in capture.host.session.manifest
    finally:
        capture.stop()


@pytest.mark.parametrize("code", ["busy", "noSources"])
def test_an_automatic_start_never_falls_back_on_busy(service, code):  # noqa: F811
    import threading

    from test_browser_service import browser

    from cassini_talk.browser_service import read_packet, request, send_packet

    server, path = service
    done = threading.Event()
    automatic = server._automatic

    def observed(*args):
        try:
            automatic(*args)
        finally:
            done.set()

    server._automatic = observed
    connection, stream = browser(path)
    message = "A recording is already running. Stop it before starting another."
    try:
        request("configure", path=path, autoRecord=True)
        command = read_packet(stream)
        assert command["action"] == "start"
        send_packet(stream, dict(type="result", requestId=command["requestId"], ok=False, error=message, code=code))
        assert done.wait(5)  # Bound only: the automatic start task signals its end.
        captures = [c for c in server.captures.values() if c.directory is not None]
        if code == "busy":
            assert not captures, "D58 #11: busy never records the meeting a second time"
        else:
            (capture,) = captures
            assert capture.host.session.manifest["refusals"] == [
                dict(atMs=0, side="browser", code="startRejected", detail="noSources", message=message)
            ]
            capture.stop()
    finally:
        stream.close()
        connection.close()


# #173 review fixes.


def recordings_started(application, snapshot):
    """main._recordings_started on a finished start-all, with a minimal application."""
    from concurrent.futures import Future
    from types import SimpleNamespace

    future = Future()
    future.set_result(snapshot)
    app = SimpleNamespace(
        _record_start_future=future,
        _record_start_group="group",
        _record_start_generation=1,
        _quitting=False,
        _capture_exiting=False,
        recording_failure=None,
        recording_error=None,
        _pending_recording=None,
        _browser_changed=lambda _snapshot: None,
        start_native=lambda: pytest.fail("A browser meeting failed: no native start"),
        get_windows=lambda: [],
        _native_changed=lambda: None,
        _show_pending_recording=lambda: None,
        release=lambda: None,
    )
    application._recordings_started(app, future, 1)
    return app


@pytest.mark.parametrize("answer", ["none", "disconnect"])
def test_an_unanswered_start_shows_the_banner_at_the_production_timeout_ratio(tmp_path, application_class, answer):  # noqa: F811
    """Production waits as long for one Start as for the whole batch (25 s each)."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from broker_fixture import stop_broker
    from test_browser_sessions import Extension
    from test_refusal_vectors import Taps

    from cassini_talk.browser_service import BrowserService, request

    Taps.refused = {}
    broker_service = BrowserService(
        tmp_path / "config.json",
        capture_parent=tmp_path / "library",
        tap_factory=Taps,
        notifier=lambda *_: None,
        command_timeout=1,
    )
    broker_service.batch_timeout = broker_service.command_timeout
    path = tmp_path / "broker/browser.sock"
    ready = threading.Event()
    worker = threading.Thread(target=broker_service.serve, args=(path, ready), daemon=True)
    worker.start()
    assert ready.wait(2)
    peer = Extension(path, (3,))
    try:
        with ThreadPoolExecutor(1) as pool:
            pending = pool.submit(request, "start-all", path=path)
            assert peer.receive()["action"] == "start"
            if answer == "disconnect":
                peer.close()
            snapshot = pending.result(10)
        assert not snapshot["cancelled"], "The deadline is not the user's cancel"
        detail = "timeout" if answer == "none" else snapshot["failures"][0]["refusals"][0].get("detail")
        assert detail in ("timeout", "disconnected")
        assert snapshot["failures"][0]["refusals"] == [dict(atMs=0, side="browser", code="startFailed", detail=detail)]
        app = recordings_started(application_class, snapshot)
        assert app.recording_failure["id"] == "errors.recordFailed.line"
        assert app.recording_failure["caption"] == "errors.refusal.noAnswer"
        assert app.recording_failure["action"] == "common.tryAgain" and app.recording_error is None
    finally:
        peer.close()
        stop_broker(broker_service, worker, path)


def test_a_cancelled_record_shows_no_banner(application_class):  # noqa: F811
    app = recordings_started(
        application_class, dict(cancelled=True, failures=[dict(refusals=[], error="x")], statuses=[], meetings=[])
    )
    assert app.recording_failure is None and app.recording_error is None


def test_a_local_fallback_failure_keeps_the_browser_refusal_first(broker, monkeypatch):  # noqa: F811
    from concurrent.futures import ThreadPoolExecutor

    from test_browser_sessions import Extension

    from cassini_talk.browser_service import request, send_packet

    def busy(*_args, **_kwargs):
        raise ValueError("A recording command is still being processed")

    monkeypatch.setattr(broker.service, "start_fallback", busy)
    peer = Extension(broker.path, (3,))
    message = "No audio is available from that meeting yet."
    try:
        with ThreadPoolExecutor(1) as pool:
            pending = pool.submit(request, "start-all", path=broker.path)
            command = peer.receive()
            result = dict(type="result", requestId=command["requestId"], ok=False, error=message, code="noSources")
            send_packet(peer.stream, result)
            snapshot = pending.result(10)
        stored = snapshot["failures"][0]["refusals"]
        assert stored == [
            dict(atMs=0, side="browser", code="startRejected", detail="noSources", message=message),
            dict(atMs=0, side="browser", code="startFailed", detail="busy"),
        ]
        assert refusals.banner(stored)["caption"] == "errors.refusal.noSources"
    finally:
        peer.close()


def test_a_folder_kept_for_typed_content_stores_each_refusal_once(tmp_path):
    import time

    from test_native_sides import Taps

    from cassini_talk.browser_capture import BrowserCapture
    from cassini_talk.meeting_store import update_metadata
    from cassini_talk.refusals import NothingOpened, from_result

    class TypedTaps(Taps):
        def __call__(self, kind, origin, append, failed, *, level):
            tap = super().__call__(kind, origin, append, failed, level=level)
            start = tap.start

            def typed():
                # The user types a title while the sides open.
                update_metadata(capture.host.session.directory, title="Weekly", titleSource="user")
                start()

            tap.start = typed
            return tap

    capture = BrowserCapture(
        lambda *_: None, parent=tmp_path, tap_factory=TypedTaps(refuse={"microphone": 1, "system": 1})
    )
    capture.browser_lost = True
    browser = from_result(dict(ok=False, code="pageUnreachable", error="No page yet"))
    message = dict(type="start", captureId="kept", timelineOriginMs=int(time.time() * 1000), microphone=True)
    with pytest.raises(NothingOpened) as raised:
        capture.start(message, others_source="system", reason="missing", refusals=[browser])
    assert [r["side"] for r in raised.value.refusals] == ["browser", "me", "others"]
    (directory,) = tmp_path.iterdir()
    saved = json.loads((directory / "session.json").read_text())
    assert [r["side"] for r in saved["refusals"]] == ["browser", "me", "others"]


def test_an_unreadable_meeting_json_keeps_the_folder(tmp_path):
    session = started(tmp_path)
    (session.directory / "meeting.json").write_text("{not json")
    assert not session.discard() and session.directory.exists() and not session.finished
    assert (session.directory / "meeting.json").read_text() == "{not json"


def test_the_folder_is_removed_under_the_metadata_lock(tmp_path, monkeypatch):
    """A title or notes save queued on the lock then finds no folder: no orphan."""
    from cassini_talk import meeting_store
    from cassini_talk.meeting_store import update_metadata

    session = started(tmp_path)
    real, held = Path.rmdir, []

    def rmdir(path, *args, **kwargs):
        if path == session.directory:
            held.append(bool(getattr(meeting_store._TRANSACTIONS, "held", None)))
        return real(path, *args, **kwargs)

    monkeypatch.setattr(Path, "rmdir", rmdir)
    assert session.discard()
    assert held == [True], "The folder is removed while the metadata lock is held"
    with pytest.raises((OSError, ValueError)):
        update_metadata(session.directory, title="Weekly sync", titleSource="user")
    assert not session.directory.exists(), "No orphan folder"


def test_app_too_old_expires_and_never_hides_a_setup_failure(broker):  # noqa: F811
    from test_browser_service import wait_for

    from cassini_talk.browser_connection import BrowserConnection
    from cassini_talk.browser_service import APP_TOO_OLD_S, request
    from cassini_talk.companion import SetupError

    connection, stream = hello(broker.path, 2)
    try:
        connection.settimeout(5)
        assert stream.read(1) == b""
    finally:
        stream.close()
        connection.close()
    wait_for(lambda: request(path=broker.path)["appTooOld"])
    broker.clock.now += APP_TOO_OLD_S + 1  # The newer companion stopped retrying.
    assert request(path=broker.path)["appTooOld"] is False

    controller = BrowserConnection(
        lambda: None, available=False, later=lambda *_: 0, post=lambda *_: 0, cancel=lambda *_: None
    )
    controller.observe(dict(appTooOld=True))
    assert controller.snapshot()["phase"] == "appTooOld"
    controller.error = SetupError("buildMissing", retryable=True)
    assert controller.snapshot()["phase"] == "failed", "A real setup failure keeps its Retry"


from test_resume_capture_host import REFUSALS as RESUME_REFUSALS  # noqa: E402


@pytest.mark.parametrize("name", [*RESUME_REFUSALS, "stop_all", "cancelled", "attached", "restored"])
def test_only_a_refused_resume_of_a_waiting_recording_is_recorded(broker, name):  # noqa: F811
    """D58 #10: recorded only while the recording waits on system for that call.

    A user Stop, Stop all, a cancelled capture, a resume for another call, an
    attached or restored capture: command scoped only, nothing in the file.
    """
    from test_capture_record import declare, pcm
    from test_resume_capture_host import ACK, lose, proxy, restored, resume, started

    client, capture = started(broker)
    assert client.handle(declare("guest", "doc-1:source:1")) == ACK
    assert client.handle(pcm("guest", 480)) == ACK
    lose(broker, client, capture)
    packet, other = {}, None
    if name in RESUME_REFUSALS:
        case = RESUME_REFUSALS[name]
        if case.get("before") is not None:
            case["before"](broker, capture)
        packet = case.get("packet", {})
        packet = packet(capture) if callable(packet) else packet
    elif name == "stop_all":
        broker.service._stopping_all.set()
    elif name == "cancelled":
        broker.service._cancelled_captures[capture.capture_id] = "user"
    else:
        other = proxy(broker)
        assert other.handle(resume(capture))["resumed"] is True
        if name == "restored":
            restored(broker, other, capture, "returned", "doc-1:source:2")
    before = list(capture.host.session.manifest.get("refusals", []))
    capture.mark_resume_ask("ask-1")  # R82 1b: refusals are recorded for an ask.
    attempt = proxy(broker)
    try:
        assert attempt.handle(resume(capture, **packet))["type"] == "error"
        after = capture.host.session.manifest.get("refusals", [])
        if name == "origin":
            (item,) = after[len(before) :]
            assert (item["side"], item["code"], item["detail"]) == ("browser", "startRejected", "resumeRefused")
        else:
            assert after == before, name
    finally:
        attempt.close()
        if other is not None:
            other.close()
        broker.service._stopping_all.clear()
        broker.service._cancelled_captures.pop(capture.capture_id, None)
        capture.stop()


def test_a_new_browser_record_clears_an_earlier_start_error(application_class):  # noqa: F811
    """R82: a stale D17 partial-start error never survives a later Record."""
    from concurrent.futures import Future
    from types import SimpleNamespace

    submitted = []
    app = SimpleNamespace(
        _record_start_future=None,
        _quitting=False,
        recording_failure=dict(id="errors.recordFailed.line"),
        recording_error="Recording could not start.",
        browser_companion_available=True,
        _record_start_generation=0,
        _record_start_group=None,
        get_windows=lambda: [],
        hold=lambda: None,
        _record_start_executor=SimpleNamespace(submit=lambda function: submitted.append(function) or Future()),
    )
    application_class.start_recordings(app)
    assert submitted and app.recording_error is None and app.recording_failure is None


@pytest.mark.parametrize("typed", [False, True], ids=["blank", "typed"])
@pytest.mark.parametrize("refused", [(), ("microphone",)], ids=["both-cancelled", "one-refused-one-cancelled"])
def test_a_stop_during_fallback_startup_leaves_no_folder_and_no_banner(tmp_path, refused, typed):
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor

    from test_native_sides import Taps

    from cassini_talk.browser_capture import BrowserCapture
    from cassini_talk.browser_service import start_refusals
    from cassini_talk.meeting_store import update_metadata
    from cassini_talk.refusals import StartCancelled, from_result

    release = threading.Event()
    entered = {"microphone": threading.Event(), "system": threading.Event()}

    class GatedTaps(Taps):
        def __call__(self, kind, origin, append, failed, *, level):
            tap = super().__call__(kind, origin, append, failed, level=level)
            start = tap.start

            def opening():
                entered[kind].set()
                start()  # Waits on its gate.

            tap.start = opening
            return tap

    taps = GatedTaps(refuse={kind: 1 for kind in refused}, gate={"microphone": release, "system": release})
    capture = BrowserCapture(lambda *_: None, parent=tmp_path, tap_factory=taps)
    capture.browser_lost = True
    browser = from_result(dict(ok=False, code="pageUnreachable", error="No page yet"))
    message = dict(type="start", captureId="stopped", timelineOriginMs=int(time.time() * 1000), microphone=True)
    with ThreadPoolExecutor(2) as pool:
        starting = pool.submit(capture.start, message, others_source="system", reason="missing", refusals=[browser])
        assert all(event.wait(5) for event in entered.values())  # Bound only: both sides are opening.
        stopping = pool.submit(capture.stop)
        assert capture.stop_requested.wait(5)  # Bound only: the user's Stop has arrived.
        directory = capture.host.session.directory
        if typed:
            # The user typed a title while the sides opened: discardBlank keeps it.
            update_metadata(directory, title="Weekly", titleSource="user")
        release.set()
        with pytest.raises(StartCancelled) as raised:
            starting.result(10)
        assert stopping.result(10) == dict(type="stopped", path=str(directory) if typed else None)
    # Nothing opened because of the Stop: no banner, and no refusal for any
    # native side. A blank folder goes; a typed one stays, with only the
    # browser's refusal from before the stand-in.
    assert start_refusals(raised.value) == [] and capture.finished
    if typed:
        assert not capture.discarded and list(tmp_path.iterdir()) == [directory]
        saved = json.loads((directory / "session.json").read_text())
        assert saved["refusals"] == [browser], saved
        # A user Stop of a recording that holds no audio, as any such Stop.
        assert (saved["state"], saved["problemKind"], saved["events"][-1]["reason"]) == ("interrupted", "empty", "user")
        assert saved["tracks"] == []
    else:
        assert capture.discarded and not list(tmp_path.iterdir())
    assert all(tap.stopped for tap in taps.made if tap.started)


def test_a_cancelled_fallback_start_keeps_no_browser_refusal_for_the_banner(broker, monkeypatch):  # noqa: F811
    from concurrent.futures import ThreadPoolExecutor

    from test_browser_sessions import Extension

    from cassini_talk.browser_service import request, send_packet
    from cassini_talk.refusals import StartCancelled

    def stopped(*_args, **_kwargs):
        raise StartCancelled("Recording start was cancelled")

    monkeypatch.setattr(broker.service, "start_fallback", stopped)
    peer = Extension(broker.path, (3,))
    try:
        with ThreadPoolExecutor(1) as pool:
            pending = pool.submit(request, "start-all", path=broker.path)
            command = peer.receive()
            result = dict(type="result", requestId=command["requestId"], ok=False, error="No page", code="noSources")
            send_packet(peer.stream, result)
            snapshot = pending.result(10)
        # A Stop is not a refusal: the browser's refusal leads no banner.
        assert snapshot["failures"][0]["refusals"] == []
    finally:
        peer.close()
