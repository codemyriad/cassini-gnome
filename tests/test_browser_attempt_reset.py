# SPDX-License-Identifier: GPL-3.0-or-later
"""R2: reconnect-stable attempt suppression must follow the live call lifecycle."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import pytest
from test_browser_capture import server  # noqa: F401
from test_browser_service import meeting, no_command, service, wait_for  # noqa: F401
from test_control_reconnect import INSTANCE, live, lose, peer

from cassini_talk.browser_service import STOPPED_CALL_GRACE, STOPPED_CALL_REPORT_WINDOW, request, send_packet

URL = meeting()["url"]


def announce(extension, *, heard=True, url=URL):
    send_packet(
        extension.stream,
        dict(
            type="meetings",
            meetings=[dict(meeting(), title=extension.title, url=url, heard=heard, pageState="present")],
        ),
    )
    wait_for(
        lambda: any(
            m["title"] == extension.title and m["url"] == url and m["heard"] is heard
            for m in request(path=extension.path)["meetings"]
        )
    )


def idle(broker):
    wait_for(lambda: not broker._scheduled_calls and not any(b.pending for b in broker.browsers.values()))


@contextmanager
def blocked_workers(broker):
    release = threading.Event()
    entered = [threading.Event() for _ in range(4)]
    for event in entered:
        broker.workers.submit(lambda event=event: (event.set(), release.wait(10)))
    assert all(event.wait(1) for event in entered)
    try:
        yield release
    finally:
        release.set()


class PolicyGate:
    """The broker's lifecycle policy, which a test can hold across a window.

    The serve loop's tick runs policy at any moment. Between a capture's
    finish and its control link's loss, a tick may auto-start the still
    announced call on the link that is about to drop; that unanswered Start is
    lost with the link, and a lost auto-start is retried only after its real
    grace. Install the gate before the window: every later lookup of
    broker.policy, including a tick's that has not yet run, goes through it.
    held() waits for a policy pass already running to finish, defers the ones
    that arrive during the window, and runs policy once after it.
    """

    def __init__(self, broker):
        self.broker, self.real = broker, broker.policy
        self.condition = threading.Condition()
        self.running, self.holding, self.deferred = 0, False, False
        broker.policy = self

    def __call__(self):
        with self.condition:
            if self.holding:
                self.deferred = True
                return
            self.running += 1
        try:
            self.real()
        finally:
            with self.condition:
                self.running -= 1
                self.condition.notify_all()

    @contextmanager
    def held(self):
        with self.condition:
            self.holding = True
            # Bound only: a running pass releases the condition when it returns.
            assert self.condition.wait_for(lambda: not self.running, 10), "policy pass did not finish"
        try:
            yield
        finally:
            with self.condition:
                self.holding, deferred, self.deferred = False, self.deferred, False
        if deferred:
            self()

    def remove(self):
        del self.broker.policy


def test_queued_heard_loss_can_later_auto_record_a_real_instance(service):  # noqa: F811
    broker, path = service
    extension = peer(path)
    try:
        with blocked_workers(broker) as release:
            request("configure", path=path, autoRecord=True)
            announce(extension, heard=False)
            release.set()
            idle(broker)
        announce(extension)
        command = extension.receive()
        assert command["action"] == "start"
        extension.answer(command)
        idle(broker)
    finally:
        extension.close()


def test_rejected_start_with_heard_loss_can_later_auto_record_a_real_instance(service):  # noqa: F811
    broker, path = service
    extension = peer(path)
    try:
        request("configure", path=path, autoRecord=True)
        first = extension.receive()
        announce(extension, heard=False)
        extension.answer(first, ok=False)
        idle(broker)
        announce(extension)
        retry = extension.receive()
        assert retry["action"] == "start"
        extension.answer(retry)
        idle(broker)
    finally:
        extension.close()


def test_direct_navigation_away_and_back_can_auto_record_the_same_url_again(service):  # noqa: F811
    broker, path = service
    extension = peer(path)
    try:
        request("configure", path=path, autoRecord=True)
        extension.answer(extension.receive())
        idle(broker)
        announce(extension, heard=False)
        extension.sessions.clear()
        send_packet(extension.stream, dict(type="status", recording=False, sessions=[]))
        wait_for(lambda: not any(s["recording"] for s in request(path=path)["statuses"]))
        announce(extension, url="https://other.example/next-call", heard=False)
        announce(extension, url=URL, heard=False)
        announce(extension)
        retry = extension.receive()
        assert retry["action"] == "start"
        extension.answer(retry)
        idle(broker)
    finally:
        extension.close()


@pytest.mark.parametrize("expiry", ["absent", "late-report"])
def test_expired_stop_does_not_keep_a_real_instances_next_join_suppressed(service, monkeypatch, expiry):  # noqa: F811
    broker, path = service
    old = peer(path)
    replacement = None
    try:
        request("configure", path=path, autoRecord=True)
        old.answer(old.receive())
        idle(broker)
        identifier = request(path=path)["meetings"][0]["id"]
        with ThreadPoolExecutor(1) as pool:
            stopped = pool.submit(request, "stop", path=path, id=identifier)
            old.answer(old.receive())
            stopped.result(1)
        idle(broker)
        if expiry == "absent":
            old.close()
            wait_for(lambda: not broker.browsers)
        previous_connections = set(broker.browsers)
        replacement = peer(path, ())

        def connected():
            with broker.lock:
                return any(
                    key not in previous_connections and browser.hello_seen and browser.status_seen
                    for key, browser in broker.browsers.items()
                )

        # An empty inventory alone is not a readiness condition: its predicate
        # is already true before the broker accepts the replacement socket.
        # Record connected_at on the real clock before advancing the deadline.
        wait_for(connected)
        actual = time.monotonic
        age = STOPPED_CALL_GRACE + 1 if expiry == "absent" else STOPPED_CALL_REPORT_WINDOW + 1
        monkeypatch.setattr(time, "monotonic", lambda: actual() + age)
        announce(replacement, heard=False)
        assert (INSTANCE, 3) not in broker.stopped_calls
        announce(replacement)
        command = replacement.receive()
        assert command["action"] == "start"
        replacement.answer(command)
        idle(broker)
    finally:
        old.close()
        if replacement is not None:
            replacement.close()


@pytest.mark.parametrize("finish_before_disconnect", [False, True])
@pytest.mark.parametrize("reason", ["callEnded", "pageGone", "appQuit"])
def test_a_retained_finished_capture_does_not_suppress_the_reconnecting_call(server, reason, finish_before_disconnect):  # noqa: F811
    broker, path, _ = server
    # Before any state the window depends on: a tick resolves policy through it.
    gate = PolicyGate(broker)
    extension, client, capture = live(broker, path)
    replacement = None
    try:
        request("configure", path=path, autoRecord=True)
        send_packet(extension.stream, dict(type="status", recording=True, sessions=list(extension.sessions.values())))
        announce(extension)
        identifier = request(path=path)["meetings"][0]["id"]
        # live()'s inventory already satisfies announce()'s predicate, so it
        # cannot show that this status and inventory were handled: wait for
        # their effect, the recording call's remembered attempt.
        wait_for(lambda: identifier in broker._attempted_calls)
        with gate.held():
            if finish_before_disconnect:
                capture.stop(reason=reason)
            lose(broker, extension)
            if not finish_before_disconnect:
                capture.stop(reason=reason)
        assert capture.finished and capture.capture_id in broker.captures
        replacement = peer(path, ())
        announce(replacement, heard=False)
        announce(replacement)
        command = replacement.receive()
        assert command["action"] == "start"
        replacement.answer(command)
        idle(broker)
    finally:
        capture.stop()
        extension.close()
        if replacement is not None:
            replacement.close()
        client.close()
        gate.remove()


def test_manual_record_during_overlap_rejects_the_old_pending_start_without_cancelling_it(service):  # noqa: F811
    broker, path = service
    broker.command_timeout = 3
    old = peer(path)
    replacement = None
    try:
        request("configure", path=path, autoRecord=True)
        first = old.receive()
        replacement = peer(path)
        identifier = request(path=path)["meetings"][0]["id"]
        old_pending = next(iter(broker._pending_calls[identifier].values()))
        with ThreadPoolExecutor(1) as pool:
            manual = pool.submit(request, "start", path=path, id=identifier)
            wait_for(
                lambda: (
                    manual.done()
                    or any(
                        b.pending
                        for b in broker.browsers.values()
                        if b.meetings.get(3, {}).get("title") == replacement.title
                    )
                )
            )
            if not manual.done():
                replacement.answer(replacement.receive())
            with pytest.raises(ValueError, match="still being processed"):
                manual.result(1)
        assert not old_pending.get("cancelled")
        assert no_command(replacement.connection, replacement.stream)
        old.answer(first)
        idle(broker)
    finally:
        old.close()
        if replacement is not None:
            replacement.close()
