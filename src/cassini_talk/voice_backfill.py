# SPDX-License-Identifier: GPL-3.0-or-later
"""Bounded, idle preparation of historical meetings; no retrospective naming."""

import logging
from collections import deque
from concurrent.futures import CancelledError, ThreadPoolExecutor

LOG = logging.getLogger(__name__)


class VoiceBackfill:
    BATCH = 8

    def __init__(self, app, *, later=None, post=None, cancel=None):
        if later is None:
            from gi.repository import GLib

            def later(callback):
                return GLib.timeout_add_seconds(5, callback, priority=GLib.PRIORITY_LOW)

            def post(callback, *args):
                return GLib.idle_add(callback, *args, priority=GLib.PRIORITY_LOW)

            cancel = GLib.source_remove
        self.app, self.later, self.post, self.cancel = app, later, post, cancel
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cassini-voice-backfill")
        self.closed = self.inspecting = self.running = self.ready = False
        self.timer = 0
        self.meetings = {}
        self.remaining = deque()
        self.attempted = set()

    def _ready(self):
        return (
            self.app.transcription_enabled
            and all(
                model is not None and model.snapshot().get("ready")
                for model in (self.app.voice_models, self.app.diarization_models)
            )
            and not self.app.voice_store.snapshot()["readOnly"]
        )

    def observe(self, snapshot):
        if self.closed or snapshot.unavailable:
            return
        candidates = {
            m.directory: m
            for m in snapshot.meetings
            if m.lifecycle == "saved"
            and m.transcript == "done"
            and m.metadata_editable
            and m.has_audio
            and m.words
            and any(s.id != "me" for s in m.speakers)
            and not self.app.meeting_trash.hides(m.directory)
        }
        changed = candidates.keys() != self.meetings.keys()
        self.meetings = candidates
        self.attempted.intersection_update(candidates)
        if changed:
            self.remaining = deque(p for p in candidates if p not in self.attempted)
        self.models_changed()

    def models_changed(self):
        ready = self._ready()
        if ready and not self.ready:
            self.attempted.clear()
            self.remaining = deque(self.meetings)
        self.ready = ready
        self._schedule()

    def _schedule(self):
        if not self.closed and self.ready and self.remaining and not (self.timer or self.inspecting or self.running):
            self.timer = self.later(self._tick)

    def _tick(self):
        self.timer = 0
        if self.closed or not self._ready():
            return False
        queue = self.app.transcriber
        if queue is not None and (queue.pending or queue.voice_pending):
            self._schedule()
            return False
        batch = [self.remaining.popleft() for _ in range(min(self.BATCH, len(self.remaining)))]
        self.inspecting = True
        future = self.executor.submit(self._inspect, batch)
        future.add_done_callback(lambda f: self.post(self._inspected, f))
        return False

    def _inspect(self, batch):
        missing = []
        for path in batch:
            if self.closed:
                break
            try:
                _cache, prepared = self.app.voice_store.preparation(path)
                if not prepared:
                    missing.append(path)
            except (OSError, ValueError):
                LOG.exception("Could not inspect historical voice cache")
        return missing

    def _inspected(self, future):
        self.inspecting = False
        if self.closed:
            return False
        try:
            missing = future.result()
        except CancelledError:
            return False
        missing = [p for p in missing if p in self.meetings and p not in self.attempted]
        self.remaining.extendleft(reversed(missing))
        if self._ready() and missing:
            path = self.remaining.popleft()
            if self.app.meeting_trash.hides(path):
                self.attempted.add(path)
            elif self.app._ensure_transcriber().backfill_voices(path, self._completed):
                self.running = True
                self.attempted.add(path)
            else:
                self.remaining.appendleft(path)
        self._schedule()
        return False

    def _completed(self, path, preempted=False):
        self.post(self._finished, path, preempted)

    def _finished(self, path, preempted=False):
        self.running = False
        if not self.closed:
            if preempted and path in self.meetings:
                self.attempted.discard(path)
                self.remaining.appendleft(path)
            self.app._transcription_changed()
            self._schedule()
        return False

    def close(self):
        self.closed = True
        if self.timer:
            self.cancel(self.timer)
            self.timer = 0
        self.executor.shutdown(wait=False, cancel_futures=True)
