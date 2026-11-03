# SPDX-License-Identifier: GPL-3.0-or-later
"""Real broker RPC: call snapshots, individual stops, cancellation and app leases."""

from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from test_browser_service import meeting, service, wait_for  # noqa: F401

from cassini_talk.browser_service import connect, read_packet, request, send_packet


class Extension:
    def __init__(self, path, tabs=(3, 4), instance=None):
        self.path = path
        self.connection = connect(path, timeout=2)
        self.stream = self.connection.makefile("rwb")
        self.sessions = {}
        send_packet(self.stream, dict(role="extension", browserInstance=instance))
        send_packet(self.stream, dict(type="hello", version=1, capabilities=["independent-sessions-v1"]))
        send_packet(self.stream, dict(type="status", recording=False, sessions=[]))
        self.inventory(tabs)
        wait_for(lambda: len(request(path=path)["meetings"]) == len(tabs))

    def inventory(self, tabs):
        send_packet(self.stream, dict(type="meetings", meetings=[meeting(tab) for tab in tabs]))

    def receive(self):
        return read_packet(self.stream)

    def answer(self, command, *, ok=True):
        tab = command["tabId"]
        if command["action"] == "start" and ok:
            value = dict(recording=True, tabId=tab, captureId=str(uuid4()), path="/private/Recording-" + str(uuid4()))
            self.sessions[tab] = value
        else:
            value = dict(self.sessions.pop(tab, dict(tabId=tab)), recording=False)
        send_packet(
            self.stream,
            dict(
                type="result",
                requestId=command["requestId"],
                ok=ok,
                error=None if ok else "Start cancelled",
                status=dict(value, sessions=list(self.sessions.values())),
            ),
        )

    def close(self):
        self.stream.close()
        self.connection.close()


def test_record_snapshots_current_calls_and_each_stop_targets_one_session(service):  # noqa: F811
    server, path = service
    peer = Extension(path)
    try:
        with ThreadPoolExecutor(1) as executor:
            started = executor.submit(request, "start-all", path=path, microphone=False)
            # The two Starts run on worker threads, so either may arrive first.
            first = peer.receive()
            assert first["tabId"] in (3, 4) and first["independent"] and not first["captureLocal"]
            assert first["microphone"] is False
            peer.inventory((3, 4, 5))  # A later call does not join manual Record.
            peer.answer(first)
            second = peer.receive()
            assert {first["tabId"], second["tabId"]} == {3, 4} and second["microphone"] is False
            assert second["independent"] and not second["captureLocal"]
            peer.answer(second)
            snapshot = started.result(2)
            assert snapshot["attempted"] == 2 and not snapshot["failures"]
            assert {s["tabId"] for s in snapshot["statuses"] if s["recording"]} == {3, 4}
            wait_for(lambda: len(request(path=path)["meetings"]) == 3)
            server.policy()
            assert all(not b.pending for b in server.browsers.values())
            identifier = next(m["id"] for m in snapshot["meetings"] if m["tabId"] == 3)
            stopped = executor.submit(request, "stop", path=path, id=identifier)
            command = peer.receive()
            assert command["tabId"] == 3
            peer.answer(command)
            assert {s["tabId"] for s in stopped.result(2)["statuses"] if s["recording"]} == {4}
            stopped_all = executor.submit(request, "stop-all", path=path)
            command = peer.receive()
            assert command["tabId"] == 4
            remaining_path = peer.sessions[4]["path"]
            peer.answer(command)
            assert stopped_all.result(2)["stoppedPaths"] == [remaining_path]
    finally:
        peer.close()


def test_auto_record_adds_later_calls_without_restarting_a_stopped_call(service):  # noqa: F811
    server, path = service
    peer = Extension(path, (3,))
    try:
        request("configure", path=path, autoRecord=True)
        peer.answer(peer.receive())
        wait_for(lambda: any(s["recording"] for s in request(path=path)["statuses"]))
        peer.inventory((3, 4))
        command = peer.receive()
        assert command["tabId"] == 4 and command["microphone"] is True
        peer.answer(command)
        wait_for(lambda: len(request(path=path)["statuses"]) == 2)
        with ThreadPoolExecutor(1) as executor:
            stopped = executor.submit(request, "stop-all", path=path)
            targets = []
            for _ in range(2):
                command = peer.receive()
                targets.append(command["tabId"])
                peer.answer(command)
            assert set(targets) == {3, 4} and not stopped.result(2)["statuses"]
        peer.inventory((3, 4))
        wait_for(lambda: not next(iter(server.browsers.values())).pending)
        server.policy()
        assert next(iter(server.browsers.values())).attempted == {3, 4}
    finally:
        peer.close()


def test_stop_cancels_a_batch_while_its_first_start_is_pending(service):  # noqa: F811
    server, path = service
    server.command_timeout = 30  # Only the Stop-all ends these Starts, never their own clock.
    peer = Extension(path)
    try:
        with ThreadPoolExecutor(2) as executor:
            started = executor.submit(request, "start-all", path=path, groupId="pending-batch")
            starts = [peer.receive(), peer.receive()]
            assert {command["tabId"] for command in starts} == {3, 4}
            stopped = executor.submit(request, "stop-all", path=path, cancelGroups=["pending-batch"])
            stops = [peer.receive(), peer.receive()]
            assert all(command["action"] == "stop" for command in stops)
            assert {command["tabId"] for command in stops} == {3, 4}
            for command in starts:
                peer.answer(command, ok=False)
            for command in stops:
                peer.answer(command)
            assert started.result(2)["cancelled"]
            assert not stopped.result(2)["statuses"]
            # Start-all can answer before a cancelled worker drops its entry.
            wait_for(lambda: not next(iter(server.browsers.values())).pending)
    finally:
        peer.close()


def test_stop_all_still_stops_a_start_that_unwound_before_its_stop_was_collected(service, monkeypatch):  # noqa: F811
    """CI's signature: one cancelled Start unwinds before Stop-all collects whom to stop.

    Stop-all cancels both pending Starts, then looks again for the Starts to
    stop. The tab-3 Start unwinds in between and drops its pending entry; the
    tab-4 Start is held after it wakes until Stop-all has collected its Stops.
    Both browsers must still get a Stop. Only the Stop-all thread is gated.
    """
    import threading

    import cassini_talk.browser_service as broker
    from cassini_talk.browser_service import BrowserService

    snapshot, command, stop_all, batch = (
        BrowserService.snapshot,
        BrowserService.command,
        BrowserService.stop_all,
        BrowserService._batch,
    )
    plain = threading.Event
    local = threading.local()
    unwound, collected, gated, batches = plain(), plain(), plain(), []

    class HeldEvent(plain):
        def wait(self, timeout=None):
            woke = super().wait(timeout)
            if woke and getattr(local, "hold", False):
                assert collected.wait(5), "Stop-all never collected its Stops"
            return woke

    def held_command(self, identifier, action, **values):
        local.hold = action == "start" and identifier.endswith(":4")
        try:
            return command(self, identifier, action, **values)
        finally:
            local.hold = False
            if action == "start" and identifier.endswith(":3"):
                unwound.set()

    def stopping(self, *args, **values):
        local.stopping = True
        try:
            return stop_all(self, *args, **values)
        finally:
            local.stopping = False

    def gated_snapshot(self):
        if getattr(local, "stopping", False) and not gated.is_set():
            gated.set()
            assert unwound.wait(5), "Stop-all did not wake the tab-3 Start"
        return snapshot(self)

    def seen_batch(self, jobs, *args, **values):
        if getattr(local, "stopping", False):
            batches.append(sorted(identifier.rpartition(":")[2] for identifier, _job in jobs))
            collected.set()
        return batch(self, jobs, *args, **values)

    monkeypatch.setattr(broker.threading, "Event", HeldEvent)
    monkeypatch.setattr(BrowserService, "command", held_command)
    monkeypatch.setattr(BrowserService, "snapshot", gated_snapshot)
    monkeypatch.setattr(BrowserService, "stop_all", stopping)
    monkeypatch.setattr(BrowserService, "_batch", seen_batch)
    test_stop_cancels_a_batch_while_its_first_start_is_pending(service)
    assert gated.is_set() and batches == [["3", "4"]], batches


def test_stop_before_start_rpc_keeps_its_cancellation_tombstone(service):  # noqa: F811
    server, path = service
    peer = Extension(path)
    try:
        request("stop-all", path=path, cancelGroups=["late-rpc"])
        result = request("start-all", path=path, groupId="late-rpc")
        assert result["cancelled"] and not result["statuses"]
        assert not next(iter(server.browsers.values())).pending
    finally:
        peer.close()


def test_quit_generation_rejects_a_late_application_lease_and_resume_allows_new_calls(service):  # noqa: F811
    server, path = service
    server.require_application = True
    peer = Extension(path, (3,))
    leases = []

    def lease(generation):
        connection = connect(path, timeout=2)
        stream = connection.makefile("rwb")
        leases.append((connection, stream))
        send_packet(stream, dict(role="application", active=True, generation=generation))
        assert read_packet(stream) == dict(ready=True, protocol=2)

    try:
        request("configure", path=path, autoRecord=True)
        assert not any(b.pending for b in server.browsers.values())
        lease(100)
        peer.answer(peer.receive())
        wait_for(lambda: bool(request(path=path)["statuses"]))
        with ThreadPoolExecutor(1) as executor:
            stopped = executor.submit(request, "stop-all", path=path, quitting=True, generation=101)
            peer.answer(peer.receive())
            assert not stopped.result(2)["statuses"]
        peer.inventory((3, 4))
        wait_for(lambda: len(request(path=path)["meetings"]) == 2)
        lease(100)  # An old handshake completing after Quit cannot reactivate policy.
        server.policy()
        assert not any(server.applications.values()) and not any(b.pending for b in server.browsers.values())
        lease(102)
        command = peer.receive()
        assert command["tabId"] == 4
        peer.answer(command)
        wait_for(lambda: any(s["tabId"] == 4 for s in request(path=path)["statuses"]))
    finally:
        for connection, stream in leases:
            stream.close()
            connection.close()
        peer.close()
