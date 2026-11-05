# SPDX-License-Identifier: GPL-3.0-or-later
"""Waits on durable work (E1, D66): each ends on the event the production code
sets, and its bound is only an upper limit sized against the fsyncs behind it.

A capture's stop or fallback, a broker shutdown that saves its open captures
and a queue job each write a folder durably. Measured on 2026-11-05 with every
os.fsync slowed by 0.15 s on a real disk, as a CI disk busy with writeback
(the commit that added this module has the method): one such wait sits through
at most 24 fsyncs (a voice job; a transcription job 22, a browser capture's save
21) and took up to 9.1 s, so the 2 s and 5 s bounds sized for in-memory state
failed. A fallback's work grows with the capture's tracks: about 6.25 fsyncs a
track plus 11. On roy's real disk one fsync takes about 0.12 s (15 in 1.86 s);
the reproduction took up to 0.38 s. FSYNC_BOUND allows 0.6 s for each. A bound
is never waited out by a passing test.
"""

import threading
import time

FSYNC_BOUND = 0.6  # seconds allowed for one fsync
DURABLE_FSYNCS = 30  # the most fsyncs one wait here sits through, with room (24 measured)
DURABLE_BOUND = FSYNC_BOUND * DURABLE_FSYNCS  # 18 s
FSYNCS_PER_TRACK = 7  # what each track adds to a fallback or a shutdown (6.25 measured)


def tracks_bound(tracks):
    """DURABLE_BOUND plus the fsyncs a capture's `tracks` add to its fallback or save."""
    return FSYNC_BOUND * (DURABLE_FSYNCS + FSYNCS_PER_TRACK * tracks)


def saved(capture, bound=DURABLE_BOUND):
    """Wait for a browser capture's stop to finish its durable save.

    BrowserCapture sets `stopped` exactly where it sets `finished`.
    """
    assert capture.stopped.wait(bound), f"capture {capture.capture_id} did not finish its save within {bound} s"
    assert capture.finished


def durable(event, what, bound=DURABLE_BOUND):
    """Wait on an Event whose setter first does durable work; `what` names it."""
    assert event.wait(bound), f"{what} not within {bound} s"


def idle(models, bound=DURABLE_BOUND):
    """Wait for a model job (a download installs its files durably) to end.

    Polled, as the one exception: SpeechModels stops calling `changed` once it
    is cancelled, so no event covers every job.
    """
    deadline = time.monotonic() + bound
    while models.snapshot()["busy"]:
        assert time.monotonic() < deadline, f"the model job did not end within {bound} s"
        time.sleep(0.01)


class Changes:
    """A change callback as an event: production code calls it on each change it
    commits, and wait(check) ends on the change that makes `check` true."""

    def __init__(self):
        self.condition = threading.Condition()

    def __call__(self, *_args):
        with self.condition:
            self.condition.notify_all()

    def wait(self, check, what="the expected state was not reached", bound=DURABLE_BOUND):
        with self.condition:
            assert self.condition.wait_for(check, bound), f"{what} within {bound} s"


class CaptureChanges(Changes):
    """A browser service's capture changes as they commit, so waits on capture
    state (a fallback's browser_lost, a save) end on the change, never a poll.

    Install it on a test-owned service before any capture exists: a capture
    keeps the callback it was created with.
    """

    def __init__(self, service):
        super().__init__()
        self.seen = []
        changed = service._capture_changed

        def observe(capture, event):
            changed(capture, event)
            with self.condition:
                self.seen.append((capture.capture_id, event, capture.browser_lost))
                self.condition.notify_all()

        service._capture_changed = observe

    def wait(self, check, what="the capture did not reach the expected state", bound=DURABLE_BOUND):
        super().wait(check, what, bound)


class Finished(list):
    """A queue's finished callback as a completion event (E1): it keeps what
    finished, in order, and wait(count) ends when the count-th job finishes."""

    def __init__(self):
        super().__init__()
        self.condition = threading.Condition()

    def __call__(self, item):
        with self.condition:
            self.append(item)
            self.condition.notify_all()

    @property
    def directories(self):
        return self

    def wait(self, count=1, bound=DURABLE_BOUND):
        """Wait for exactly `count` finished jobs."""
        with self.condition:
            assert self.condition.wait_for(lambda: len(self) >= count, bound), (
                f"the queue did not finish {count} job(s) within {bound} s ({len(self)} finished)"
            )
            assert len(self) == count, f"{len(self)} jobs finished, not {count}"
