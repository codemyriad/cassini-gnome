# SPDX-License-Identifier: GPL-3.0-or-later
"""D52.5 ask-once resume, app side: the real policy, control handler and capture."""

import logging
import threading
import time

import pytest
from test_browser_capture import Tap

from cassini_talk.browser_capture import BrowserCapture
from cassini_talk.browser_service import AudioTransport, BrowserConnection, BrowserService
from cassini_talk.session_events import source_state

CAPABILITIES = [
    "independent-sessions-v1",
    "call-evidence-v1",
    "page-state-v1",
    "multi-stream-v1",
    "resume-capture-v1",
    "sender-me-v1",
]


@pytest.fixture
def policy(tmp_path, monkeypatch):
    service = BrowserService(tmp_path / "settings.json", notifier=lambda *_: None)
    tasks, sent = [], []
    monkeypatch.setattr(service.workers, "submit", lambda function, *args: tasks.append((function, args)))
    monkeypatch.setattr(BrowserConnection, "send", lambda peer, packet: sent.append((peer, packet)))
    yield service, tasks, sent, tmp_path / "Library"
    service.workers.shutdown(wait=True)


def run(tasks):
    """The worker pool: run queued work, including work it queues."""
    while tasks:
        function, arguments = tasks.pop(0)
        function(*arguments)


def peer(service, instance="resume-browser", capabilities=CAPABILITIES):
    browser = BrowserConnection(None, browser_instance=instance)
    service.browsers[browser.id] = browser
    service.update(browser, dict(type="hello", version=1, capabilities=capabilities))
    return browser


def close(service, browser):
    with service.lock:
        browser.closed = True
        service.browsers.pop(browser.id, None)


def row(**changes):
    return dict(
        dict(
            tabId=3,
            title="Synthetic meeting",
            url="https://meet.example/call",
            sourceCount=1,
            heard=True,
            callGeneration="call-A",
            pageState="present",
        ),
        **changes,
    )


def session(**changes):
    return dict(dict(recording=True, tabId=3, captureId="F", pageState="present", callGeneration="call-A"), **changes)


def scheduled_asks(tasks):
    """Lifecycle work policy queued to ask: a decision, sent or not."""
    return [arguments for _function, arguments in tasks if arguments[-1] == "askResume"]


def asks(sent):
    return [packet for _peer, packet in sent if "resumeCapture" in packet]


def fresh_starts(sent):
    return [packet for _peer, packet in sent if packet.get("action") == "start" and "resumeCapture" not in packet]


def recording(service, parent, browser, **meeting):
    """F records call-A in tab 3 under this peer; the caller closes its transport."""
    capture = BrowserCapture(service._capture_changed, parent=parent, tap_factory=Tap)
    capture.start(dict(type="start", captureId="F", callGeneration="call-A", timelineOriginMs=int(time.time() * 1000)))
    service.captures["F"] = capture
    service.update(browser, dict(type="meetings", meetings=[row(**meeting)]))
    service.update(browser, dict(type="status", recording=True, sessions=[session()]))
    assert capture.lifecycle_capable and capture.multistream_capable
    return capture


def released(service, browser):
    """The extension deleted its session after the native port closed."""
    service.update(browser, dict(type="status", recording=False, sessions=[]))


def test_ask_waits_until_the_extension_releases_the_tab(policy):
    service, tasks, sent, parent = policy
    browser = peer(service)
    capture = recording(service, parent, browser)
    try:
        capture.close()
        assert capture.browser_lost
        # background.js stop() drains the old session for up to 15 s and
        # refuses a Start for that tab meanwhile; the only ask must wait.
        service.update(browser, dict(type="status", recording=True, sessions=[session(stopping=True)]))
        for _ in range(3):
            service.update(browser, dict(type="meetings", meetings=[row()]))
            service.policy()
            assert not scheduled_asks(tasks), "no ask is even decided while the tab is held"
            run(tasks)
        assert not asks(sent)
        released(service, browser)
        run(tasks)
        (packet,) = asks(sent)
        assert packet == dict(
            type="command",
            requestId=packet["requestId"],
            action="start",
            mode="tracks",
            tabId=3,
            independent=True,
            captureLocal=True,
            callGeneration="call-A",
            keepOnNavigation=True,
            resumeCapture=dict(
                captureId="F",
                timelineOriginMs=capture.origin_ms,
                lostAtMs=source_state(capture.host.session.manifest["events"]).since_ms,
            ),
        )
        assert "microphone" not in packet
        for _ in range(3):
            service.update(browser, dict(type="meetings", meetings=[row()]))
            released(service, browser)
            service.policy()
            run(tasks)
        assert len(asks(sent)) == 1 and not fresh_starts(sent)
        assert not capture.finished and capture.browser_lost, "an ask is never a Stop"
    finally:
        capture.stop()


def test_no_ask_while_the_transport_is_alive_on_a_control_loss_fallback(policy):
    service, tasks, sent, parent = policy
    browser = peer(service)
    capture = recording(service, parent, browser)
    try:
        # Control loss committed system/lost; the audio transport never closed.
        capture.fallback()
        assert capture.browser_lost and capture.audio_connected
        released(service, browser)
        for _ in range(3):
            service.update(browser, dict(type="meetings", meetings=[row()]))
            service.policy()
            assert not scheduled_asks(tasks), "an attached transport is never asked about"
            run(tasks)
        assert not asks(sent)
        assert not service._capture_lifecycles["F"].asked_on, "the loss's only ask is still unspent"
        # Now the transport closes too, within the same loss: that is the ask.
        capture.close()
        service.policy()
        run(tasks)
        assert len(asks(sent)) == 1
    finally:
        capture.stop()


def test_refused_resume_is_never_retried_or_turned_into_a_fresh_start(policy, monkeypatch, caplog):
    service, tasks, sent, parent = policy
    service.auto_record = True
    monkeypatch.setattr(service, "start_fallback", lambda *_args, **_kwargs: pytest.fail("a fresh fallback Start"))
    browser = peer(service)
    capture = recording(service, parent, browser)
    try:
        capture.close()
        released(service, browser)
        run(tasks)
        (packet,) = asks(sent)
        with caplog.at_level(logging.INFO, logger="cassini_talk.browser_service"):
            service.update(
                browser,
                dict(type="result", requestId=packet["requestId"], ok=False, error="Target is no longer active."),
            )
        assert "did not resume recording F: Target is no longer active." in caplog.text
        assert not browser.resumes
        clock = time.monotonic()
        monkeypatch.setattr("time.monotonic", lambda: clock)
        for minute in range(10):
            clock += 60
            service.update(browser, dict(type="meetings", meetings=[row()]))
            released(service, browser)
            service.tick()
            run(tasks)
            assert len(asks(sent)) == 1, minute
        assert not fresh_starts(sent) and "F" not in service._cancelled_captures
        assert not capture.finished and capture.browser_lost
        # A new control connection (a new hello) asks once more.
        close(service, browser)
        returning = peer(service)
        service.update(returning, dict(type="meetings", meetings=[row()]))
        released(service, returning)
        for _ in range(3):
            service.policy()
            run(tasks)
        assert len(asks(sent)) == 2 and sent[-1][0] is returning
        assert not fresh_starts(sent)
    finally:
        capture.stop()


def test_instance_less_companion_finds_its_lost_recording_by_call(policy):
    service, tasks, sent, parent = policy
    service.auto_record = True
    first = peer(service, instance=None)
    capture = recording(service, parent, first)
    try:
        assert capture.call_tab == 3
        capture.close()
        close(service, first)
        # Without browserInstance nothing binds the new connection to the old
        # one: only (tabId, callGeneration) names the call.
        second = peer(service, instance=None)
        service.update(second, dict(type="status", recording=False, sessions=[]))
        service.update(second, dict(type="meetings", meetings=[row()]))
        (shown,) = service.snapshot()["meetings"]
        assert shown["recording"] is True, "the lost call's row shows Stop"
        assert service.start_all()["attempted"] == 0
        with pytest.raises(ValueError):
            service.command(f"{second.id}:3", "start", user=True)
        service.policy()
        run(tasks)
        (packet,) = asks(sent)
        assert sent[-1][0] is second and packet["resumeCapture"]["captureId"] == "F"
        assert not fresh_starts(sent) and list(service.captures) == ["F"]
    finally:
        capture.stop()


def test_dropped_policy_task_does_not_spend_the_ask(policy):
    service, tasks, sent, parent = policy
    browser = peer(service)
    capture = recording(service, parent, browser)
    try:
        capture.close()
        with service.lock:
            service._capture_policy_pending.add("F")  # A lifecycle task is still running.
        released(service, browser)
        service.policy()
        assert not tasks and not service._capture_lifecycles["F"].asked_on
        with service.lock:
            service._capture_policy_pending.discard("F")
        service.policy()
        run(tasks)
        assert len(asks(sent)) == 1
    finally:
        capture.stop()


def test_a_fresh_manual_record_after_the_user_stops_is_an_ordinary_start(policy, monkeypatch):
    service, tasks, sent, parent = policy
    browser = peer(service)
    capture = recording(service, parent, browser)
    capture.close()
    released(service, browser)
    run(tasks)
    assert len(asks(sent)) == 1
    with pytest.raises(ValueError):
        service.command(f"{browser.id}:3", "start", user=True)
    service.command("capture:F", "stop", user=True)
    assert capture.finished
    sent.clear()

    def answer(peer, packet):
        sent.append((peer, packet))
        service.update(peer, dict(type="result", requestId=packet["requestId"], ok=True, status=dict(recording=False)))

    monkeypatch.setattr(BrowserConnection, "send", answer)
    service.command(f"{browser.id}:3", "start", user=True)
    (packet,) = fresh_starts(sent)
    assert "resumeCapture" not in packet and packet["tabId"] == 3 and not asks(sent)


def test_a_retired_command_result_is_no_news_about_a_document(policy):
    """A late result for another tab re-reports this tab's retained state. Its
    old documentGeneration must not retire the live document's spans."""
    service, _tasks, _sent, parent = policy
    browser = peer(service)
    capture = recording(service, parent, browser)
    try:
        service.update(browser, dict(type="status", recording=True, sessions=[session(documentGeneration="doc-1")]))
        assert capture.document_generation == "doc-1"
        capture.handle(dict(type="track", id="live", kind="remote", label="Guest", sampleRate=48000, channels=1))
        service.update(browser, dict(type="meetings", meetings=[row(documentGeneration="doc-2")]))
        assert capture.document_generation == "doc-2" and not capture.streams.tracks["live"].dormant
        with service.lock:
            browser.commands["late"] = dict(tabId=4, action="stop", serial=0)
        service.update(
            browser, dict(type="result", requestId="late", ok=True, status=dict(recording=False, sessions=[]))
        )
        assert browser.states[3]["captureId"] == "F"
        assert not capture.streams.tracks["live"].dormant and capture.document_generation == "doc-2"
    finally:
        capture.stop()


def test_a_restore_refused_by_the_track_limit_is_not_retried_every_tick(policy, caplog):
    """The 64-track format cap counts every WAV of the session. A restore that
    cannot fit stays on computer sound for this loss, logged once."""
    from cassini_talk.browser_service import AudioTransport

    service, tasks, sent, parent = policy
    browser = peer(service)
    capture = recording(service, parent, browser)
    try:
        session = capture.host.session
        for index in range(64 - len(session.writers) - 1):
            session.add_track(f"earlier-{index}", "Earlier span", "remote", 48000, 1)
            session.end_track(f"earlier-{index}")
        capture.close()  # The loss adds the system WAV: 64 tracks.
        assert capture.browser_lost and len(session.writers) == 64
        released(service, browser)
        run(tasks)
        assert len(asks(sent)) == 1
        service._resume_attach(
            dict(
                type="resume",
                version=1,
                independent=True,
                captureId="F",
                timelineOriginMs=capture.origin_ms,
                callGeneration="call-A",
                features=["resume-capture-v1"],
            ),
            AudioTransport(None, service.clock),
        )
        capture.handle(dict(type="track", id="back", kind="remote", label="Guest", sampleRate=48000, channels=1))
        capture.handle(dict(type="audio", id="back", sequence=0, offsetMs=capture.elapsed_ms(), pcm="AQACAA=="))
        assert capture.browser_ready
        with caplog.at_level(logging.WARNING, logger="cassini_talk.browser_capture"):
            service.policy()
            assert [arguments[-1] for _function, arguments in tasks] == ["restore"]
            run(tasks)
            assert capture.browser_lost and capture.restore_blocked
            for _ in range(3):
                service.update(browser, dict(type="meetings", meetings=[row()]))
                service.policy()
                assert not tasks, "no restore is queued again for this loss"
        assert len([r for r in caplog.records if "cannot be restored" in r.getMessage()]) == 1
    finally:
        capture.stop()


def test_a_late_result_for_another_tab_does_not_close_the_release_gate(policy):
    """A retired command's result is merged with GNOME's own retained rows,
    the lost recording's included. That is not the extension's inventory."""
    service, tasks, sent, parent = policy
    browser = peer(service)
    capture = recording(service, parent, browser, heard=False)
    try:
        capture.close()
        released(service, browser)
        run(tasks)
        assert not asks(sent) and not browser.reported, "not heard yet"
        with service.lock:
            browser.commands["late"] = dict(tabId=4, action="stop", serial=0)
        service.update(
            browser, dict(type="result", requestId="late", ok=True, status=dict(recording=False, sessions=[]))
        )
        assert browser.states[3]["captureId"] == "F" and not browser.reported
        service.update(browser, dict(type="meetings", meetings=[row(heard=True)]))
        run(tasks)
        assert len(asks(sent)) == 1
    finally:
        capture.stop()


def test_returning_to_an_earlier_open_connection_does_not_ask_it_again(policy):
    """Asked on A, then on B (a newer connection of the same browser while A
    is still open). When B goes away, policy falls back to A for the same
    loss: A was already asked."""
    service, tasks, sent, parent = policy
    first = peer(service)
    capture = recording(service, parent, first)
    try:
        capture.close()
        released(service, first)
        run(tasks)
        assert [peer for peer, packet in sent if "resumeCapture" in packet] == [first]
        second = peer(service)
        service.update(second, dict(type="meetings", meetings=[row()]))
        released(service, second)
        run(tasks)
        assert [peer for peer, packet in sent if "resumeCapture" in packet] == [first, second]
        close(service, second)
        for _ in range(3):
            service.update(first, dict(type="meetings", meetings=[row()]))
            service.policy()
            run(tasks)
        assert len(asks(sent)) == 2, "never twice per loss on one connection"
    finally:
        capture.stop()


def a_new_loss(service, browser, capture):
    """The browser source comes back and is lost again: loss 2, same connection."""
    from cassini_talk.browser_service import AudioTransport

    service._resume_attach(
        dict(
            type="resume",
            version=1,
            independent=True,
            captureId="F",
            timelineOriginMs=capture.origin_ms,
            callGeneration="call-A",
            features=["resume-capture-v1"],
        ),
        AudioTransport(None, service.clock),
    )
    capture.handle(dict(type="track", id="back", kind="remote", label="Guest", sampleRate=48000, channels=1))
    capture.handle(dict(type="audio", id="back", sequence=0, offsetMs=capture.elapsed_ms(), pcm="AQACAA=="))
    capture.restore()
    assert not capture.browser_lost
    capture.close()
    assert capture.browser_lost and capture.loss_serial == 2


# What may change between the tick that decided an ask and the worker sending it.
CHANGES = {
    # Codex's sequence on #169: the page navigates and its count becomes unknown.
    "navigating-unknown-zero": lambda service, browser, capture: service.update(
        browser, dict(type="meetings", meetings=[row(pageState="navigating", sourceCount=0, sourceCountKnown=False)])
    ),
    "present-zero-count": lambda service, browser, capture: service.update(
        browser, dict(type="meetings", meetings=[row(sourceCount=0)])
    ),
    # One condition at a time: the page alone, then the count's knowledge alone.
    "navigating-only": lambda service, browser, capture: service.update(
        browser, dict(type="meetings", meetings=[row(pageState="navigating")])
    ),
    "cached-count-marked-unknown": lambda service, browser, capture: service.update(
        browser, dict(type="meetings", meetings=[row(sourceCountKnown=False)])
    ),
    "tab-reported-again": lambda service, browser, capture: service.update(
        browser, dict(type="status", recording=True, sessions=[session(stopping=True)])
    ),
    "a-new-loss": a_new_loss,
}
# What makes the ask eligible again afterwards, for that same connection.
EASED = {
    "navigating-unknown-zero": lambda service, browser: service.update(
        browser, dict(type="meetings", meetings=[row()])
    ),
    "present-zero-count": lambda service, browser: service.update(browser, dict(type="meetings", meetings=[row()])),
    "navigating-only": lambda service, browser: service.update(browser, dict(type="meetings", meetings=[row()])),
    "cached-count-marked-unknown": lambda service, browser: service.update(
        browser, dict(type="meetings", meetings=[row()])
    ),
    "tab-reported-again": released,
    "a-new-loss": lambda service, browser: service.policy(),
}


def queued_ask(service, parent, tasks, sent):
    """Decide an ask on one tick and leave it queued, as a busy worker pool would."""
    browser = peer(service)
    capture = recording(service, parent, browser)
    capture.close()
    released(service, browser)
    assert [arguments[-1] for _function, arguments in tasks] == ["askResume"] and not asks(sent)
    assert service._capture_lifecycles["F"].asked_on == {browser.id}
    return browser, capture


@pytest.mark.parametrize("change", CHANGES)
def test_an_ask_that_is_no_longer_eligible_when_sent_is_not_spent(policy, change):
    service, tasks, sent, parent = policy
    browser, capture = queued_ask(service, parent, tasks, sent)
    try:
        CHANGES[change](service, browser, capture)
        run(tasks)  # The worker finally sends the ask decided earlier.
        assert not asks(sent), "nothing is sent for a decision that no longer holds"
        assert browser.id not in service._capture_lifecycles["F"].asked_on, "the reservation is released"
        assert not browser.resumes
    finally:
        capture.stop()


@pytest.mark.parametrize("change", CHANGES)
def test_once_eligible_again_the_same_connection_asks_exactly_once(policy, change):
    service, tasks, sent, parent = policy
    browser, capture = queued_ask(service, parent, tasks, sent)
    try:
        CHANGES[change](service, browser, capture)
        run(tasks)
        assert not asks(sent)
        EASED[change](service, browser)
        run(tasks)
        for _ in range(3):
            service.update(browser, dict(type="meetings", meetings=[row()]))
            service.policy()
            run(tasks)
        (packet,) = asks(sent)
        assert sent[-1][0] is browser
        assert packet["resumeCapture"] == dict(
            captureId="F",
            timelineOriginMs=capture.origin_ms,
            lostAtMs=source_state(capture.host.session.manifest["events"]).since_ms,
        )
    finally:
        capture.stop()


class HeldFallback:
    """Native taps whose next system device holds its first buffer, then delivers or fails."""

    def __init__(self, fails):
        self.fails, self.armed = fails, False
        self.opening, self.first_buffer = threading.Event(), threading.Event()

    def __call__(self, kind, *args, **kwargs):
        tap = Tap(kind, *args, **kwargs)
        if kind == "system" and self.armed:
            self.armed, opened = False, tap.start

            def start():
                self.opening.set()
                if not self.first_buffer.wait(10) or self.fails:
                    raise OSError("The fallback device failed before its first buffer")
                opened()

            tap.start = start
        return tap


@pytest.mark.parametrize("instance", ["resume-browser", None])
@pytest.mark.parametrize("hello", [False, True], ids=["same-connection", "new-hello"])
@pytest.mark.parametrize("fails", [False, True], ids=["commits", "fails"])
def test_no_ask_before_the_fallback_commits_or_after_it_fails(policy, instance, hello, fails):
    """D52.5: the transport's teardown decides the fallback; only its commit makes an ask."""
    service, tasks, sent, parent = policy
    browser = peer(service, instance=instance)
    capture = recording(service, parent, browser)
    held = capture.tap_factory = HeldFallback(fails)
    transport = AudioTransport(None, service.clock)
    transport.capture = capture
    service._audio_transports.add(transport)
    held.armed = True
    # As on the transport's reader thread, while control keeps running.
    closer = threading.Thread(target=service._audio_closed, args=(transport, capture))
    closer.start()

    def lost():
        current = source_state(capture.host.session.manifest.get("events"))
        return (current.source, current.reason) == ("system", "lost")

    def ticks(connection):
        for _ in range(3):
            released(service, connection)
            service.update(connection, dict(type="meetings", meetings=[row()]))
            service.policy()
            run(tasks)

    try:
        assert held.opening.wait(10)
        ticks(browser)
        if hello:
            close(service, browser)
            browser = peer(service, instance=instance)
            ticks(browser)
        assert closer.is_alive() and not capture.browser_lost and not lost()
        assert not asks(sent), "asked before the fallback committed"
        # Decided, not committed: the call is still being recorded.
        (shown,) = service.snapshot()["meetings"]
        assert shown["recording"] is True and service.start_all()["attempted"] == 0
        held.first_buffer.set()
        closer.join(10)
        assert not closer.is_alive()
        ticks(browser)
        if fails:
            assert capture.finished and capture.end_reason == "error" and not lost()
            assert not asks(sent), "a fallback that never committed is never asked to resume"
        else:
            assert capture.browser_lost and lost()
            (packet,) = asks(sent)
            assert sent[-1][0] is browser and packet["resumeCapture"]["captureId"] == "F"
        assert not fresh_starts(sent) and list(service.captures) == ["F"]
    finally:
        held.first_buffer.set()
        closer.join(10)
        capture.stop()


def test_resume_names_the_exact_committed_seam_even_when_output_time_leads_request(policy, monkeypatch):
    service, tasks, sent, parent = policy
    browser = peer(service)
    capture = recording(service, parent, browser)
    now = [100.0]
    monkeypatch.setattr(capture, "elapsed_ms", lambda: now[0])
    try:
        # A wire packet is timestamped at its beginning. Its final frame can
        # lead the clock while fallback commits at the accepted PCM boundary.
        import base64

        capture.handle(dict(type="track", id="me", label="Me", kind="local", sampleRate=48000, channels=1))
        capture.handle(
            dict(type="audio", id="me", sequence=0, offsetMs=now[0], pcm=base64.b64encode(b"\x01\x00" * 480).decode())
        )
        capture.close()
        cut = source_state(capture.host.session.manifest["events"]).since_ms
        assert cut > now[0]
        released(service, browser)
        run(tasks)
        (packet,) = asks(sent)
        assert packet["resumeCapture"]["lostAtMs"] == cut
        assert packet["resumeCapture"]["lostAtMs"] > now[0], "the committed seam is not the request clock"
        service.policy()
        run(tasks)
        assert len(asks(sent)) == 1, "the same loss still asks exactly once"
    finally:
        capture.stop()
