# SPDX-License-Identifier: GPL-3.0-or-later
"""R94: actual manifest-directory durability precedes every resume dispatch."""

import errno
import json
import os
import stat
import threading

import pytest
from durable import DURABLE_BOUND
from test_browser_resume_policy import (
    asks,
    close,
    peer,
    policy,  # noqa: F401
    queued_ask,
    recording,
    released,
    row,
    run,
)
from test_capture_record import declare, pcm

from cassini_talk.browser_service import BrowserConnection
from cassini_talk.session_events import source_state


class DirectoryBarrier:
    """Hold the final directory fsync after the manifest rename, once."""

    def __init__(self, monkeypatch, capture, *, fails=False):
        self.entered, self.release, self.committed = (threading.Event() for _ in range(3))
        self.reference = None
        self.capture, self.fails = capture, fails
        self.armed = True
        self.real_fsync = os.fsync
        self.inode = capture.directory.stat().st_ino
        monkeypatch.setattr(os, "fsync", self.fsync)

    def fsync(self, descriptor):
        info = os.fstat(descriptor)
        if self.armed and stat.S_ISDIR(info.st_mode) and info.st_ino == self.inode:
            current = source_state(self.capture.host.session.manifest.get("events"))
            if current.source == "system" and current.reason == "lost":
                self.armed = False
                # The renamed file is visible, but not yet durably committed.
                disk = json.loads((self.capture.directory / "session.json").read_text())
                self.reference = source_state(disk["events"]).since_ms
                self.entered.set()
                assert self.release.wait(DURABLE_BOUND), "test did not release the directory barrier"
                if self.fails:
                    raise OSError(errno.EIO, "Generated loss-checkpoint directory fsync failure")
                result = self.real_fsync(descriptor)
                self.committed.set()
                return result
        return self.real_fsync(descriptor)


def worker(function):
    errors = []

    def perform():
        try:
            function()
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=perform)
    thread.start()
    return thread, errors


def ticks(service, tasks, browser):
    released(service, browser)
    service.update(browser, dict(type="meetings", meetings=[row()]))
    service.policy()
    run(tasks)


@pytest.mark.parametrize("outcome", ["commits", "fails", "stop"])
@pytest.mark.parametrize("reconnect", [False, True])
def test_resume_copies_only_a_durable_exact_loss_at_the_real_writer_barrier(
    policy,  # noqa: F811
    monkeypatch,
    outcome,
    reconnect,
):
    service, tasks, sent, parent = policy
    browser = peer(service)
    capture = recording(service, parent, browser)
    # The placed seam leads request time by 13.29 ms, the R94 failure shape.
    monkeypatch.setattr(capture, "elapsed_ms", lambda: 96013.86)
    assert capture.handle(declare("remote")) == dict(type="ack")
    assert capture.handle(pcm("remote", 480, offset=96017.15)) == dict(type="ack")
    barrier = DirectoryBarrier(monkeypatch, capture, fails=outcome == "fails")
    real_send = BrowserConnection.send

    def send(connection, packet):
        if "resumeCapture" in packet:
            assert barrier.committed.is_set(), "resume dispatched before directory durability"
            assert packet["resumeCapture"]["lostAtMs"] == barrier.reference
            assert packet["resumeCapture"]["lostAtMs"] != 96013.86
        real_send(connection, packet)

    monkeypatch.setattr(BrowserConnection, "send", send)
    # Document observation intentionally waits outside the broker lock. Signal
    # that inventory has been admitted, rather than waiting for that callback.
    observed = threading.Event()
    observe = service._observe_document

    def observe_document(*args):
        observed.set()
        return observe(*args)

    monkeypatch.setattr(service, "_observe_document", observe_document)
    closer, errors = worker(capture.close)
    readers = []
    stopper = None
    try:
        assert barrier.entered.wait(DURABLE_BOUND)
        assert barrier.reference == 96027.15 and not barrier.committed.is_set()
        # The broker must still process status/policy/Stop while storage is held.
        tick = worker(lambda: ticks(service, tasks, browser))
        readers.append(tick)
        assert observed.wait(DURABLE_BOUND)
        service.policy()
        run(tasks)
        if reconnect:
            close(service, browser)
            browser = peer(service)
            observed.clear()
            readers.append(worker(lambda: ticks(service, tasks, browser)))
            assert observed.wait(DURABLE_BOUND)
            service.policy()
            run(tasks)
        assert not asks(sent), "the renamed but uncommitted loss must not trigger an ask"
        if outcome == "stop":
            stopper, stop_errors = worker(capture.stop)
            assert capture.stop_requested.wait(DURABLE_BOUND)
        barrier.release.set()
        closer.join(DURABLE_BOUND)
        assert not closer.is_alive() and not errors
        for reader, reader_errors in readers:
            reader.join(DURABLE_BOUND)
            assert not reader.is_alive() and not reader_errors
        if stopper is not None:
            stopper.join(DURABLE_BOUND)
            assert not stopper.is_alive() and not stop_errors
        ticks(service, tasks, browser)
        if outcome == "commits":
            (packet,) = asks(sent)
            assert packet["resumeCapture"]["lostAtMs"] == barrier.reference
            # A reconnect asks again, naming the very same committed fraction.
            close(service, browser)
            browser = peer(service)
            ticks(service, tasks, browser)
            assert [p["resumeCapture"]["lostAtMs"] for p in asks(sent)] == [96027.15, 96027.15]
        else:
            assert not asks(sent)
            if outcome == "fails":
                assert not barrier.committed.is_set()
                assert source_state(capture.host.session.manifest.get("events")).reason != "lost"
                assert capture.finished
            else:
                assert capture.finished and capture.stop_requested.is_set()
    finally:
        barrier.release.set()
        closer.join(DURABLE_BOUND)
        for reader, _reader_errors in readers:
            reader.join(DURABLE_BOUND)
        if stopper is not None:
            stopper.join(DURABLE_BOUND)
        capture.stop()


@pytest.mark.parametrize("stop_during_checkpoint", [False, True])
def test_queued_resume_revalidates_after_a_busy_checkpoint_without_blocking_stop(
    policy,  # noqa: F811
    monkeypatch,
    stop_during_checkpoint,
):
    service, tasks, sent, parent = policy
    browser, capture = queued_ask(service, parent, tasks, sent)
    barrier = DirectoryBarrier(monkeypatch, capture)

    def checkpoint():
        with capture.lock:
            capture.host.session.checkpoint()

    writer, errors = worker(checkpoint)
    try:
        assert barrier.entered.wait(DURABLE_BOUND)
        dispatch, dispatch_errors = worker(lambda: run(tasks))
        dispatch.join(DURABLE_BOUND)
        assert not dispatch.is_alive() and not dispatch_errors
        assert not asks(sent) and not browser.resumes and not capture.resume_asks
        assert browser.id not in service._capture_lifecycles["F"].asked_on
        if stop_during_checkpoint:
            capture.request_stop()
            assert capture.stop_requested.is_set()
        barrier.release.set()
        writer.join(DURABLE_BOUND)
        assert not writer.is_alive() and not errors
        ticks(service, tasks, browser)
        if stop_during_checkpoint:
            assert not asks(sent)
        else:
            (packet,) = asks(sent)
            assert packet["resumeCapture"]["lostAtMs"] == barrier.reference
    finally:
        barrier.release.set()
        writer.join(DURABLE_BOUND)
        capture.stop()
