# SPDX-License-Identifier: GPL-3.0-or-later
"""D58 (R79) refusals on GNOME: writer rules, no folder, captureRecord, browser
start failures, a refused resume, the hello version and the live copy."""

import json
import logging
from pathlib import Path

import pytest
from test_resume_capture_host import broker  # noqa: F401

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
    lines = refusals.live_lines(stored, [("system", True)])
    assert [(line["side"], line["id"], line["caption"]) for line in lines] == [
        ("browser", "errors.othersFallback.line", "errors.refusal.noSources"),
        ("me", "errors.micPermission.line", None),
        ("others", "errors.deviceUnavailable.others", None),
    ]
    # A side that records now shows nothing.
    assert [line["side"] for line in refusals.live_lines(stored, [("microphone", None), ("remote", False)])] == [
        "others"
    ]
    # A side turned off after its refusal shows nothing; a refused turn-on does.
    off = [dict(type="selection", atMs=7000, me=False, others=True, reason="user")]
    assert [line["side"] for line in refusals.live_lines(stored, [], off)] == ["browser", "others"]
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
