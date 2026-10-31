# SPDX-License-Identifier: GPL-3.0-or-later
"""Historical jobs are bounded and yield to foreground work; no GTK needed."""

from pathlib import Path
from types import SimpleNamespace as S

from test_transcription import until

from cassini_talk.voice_backfill import VoiceBackfill


def test_bounded_idle_scan_waits_for_both_models_and_never_runs_two_jobs():
    ready = False
    inspected, admitted, callbacks = [], [], []
    posted, timers = [], []
    model = S(snapshot=lambda: dict(ready=ready))

    def preparation(path):
        inspected.append(path)
        return {}, path.name != "old"

    def admit(path, callback):
        admitted.append(path)
        callbacks.append(callback)
        return True

    queue = S(pending=set(), voice_pending=set(), backfill_voices=admit)
    app = S(
        transcription_enabled=True,
        voice_models=model,
        diarization_models=model,
        voice_store=S(preparation=preparation, snapshot=lambda: dict(readOnly=False)),
        transcriber=queue,
        _ensure_transcriber=lambda: queue,
        meeting_trash=S(hides=lambda _: False),
        _transcription_changed=lambda: None,
    )
    controller = VoiceBackfill(
        app,
        later=lambda f: timers.append(f) or len(timers),
        post=lambda f, *a: posted.append((f, a)),
        cancel=lambda _: None,
    )
    meetings = tuple(
        S(
            directory=Path(name),
            lifecycle="saved",
            transcript="done",
            metadata_editable=True,
            has_audio=True,
            words=(S(speaker="others"),),
            speakers=(S(id="others"),),
        )
        for name in [str(i) for i in range(18)] + ["old"]
    )
    try:
        controller.observe(S(unavailable=False, meetings=meetings))
        assert not timers and not inspected
        ready = True
        controller.models_changed()
        assert len(timers) == 1
        # Foreground jobs prevent even cache inspection.
        queue.pending.add(Path("foreground"))
        timers.pop(0)()
        assert not inspected and len(timers) == 1
        queue.pending.clear()
        while not admitted:
            before_batch = len(inspected)
            timers.pop(0)()
            until(lambda: bool(posted))
            f, args = posted.pop(0)
            before = len(inspected)
            assert before - before_batch <= controller.BATCH
            f(*args)
            assert len(inspected) == before
            assert before <= 19
        assert admitted == [Path("old")]
        assert controller.running and not timers
        controller.models_changed()
        assert not timers  # Ready/progress updates cannot submit another job.
        callbacks.pop()(Path("old"), False)
        f, args = posted.pop(0)
        f(*args)
        assert not controller.running and not timers
        assert set(inspected) == {m.directory for m in meetings}
    finally:
        controller.close()


def test_preempted_job_is_retried_but_failure_is_bounded_per_launch():
    timers, posted, callbacks = [], [], []
    model = S(snapshot=lambda: dict(ready=True))
    queue = S(pending=set(), voice_pending=set())
    queue.backfill_voices = lambda path, callback: callbacks.append(callback) or True
    app = S(
        transcription_enabled=True,
        voice_models=model,
        diarization_models=model,
        voice_store=S(preparation=lambda _: ({}, False), snapshot=lambda: dict(readOnly=False)),
        transcriber=queue,
        _ensure_transcriber=lambda: queue,
        meeting_trash=S(hides=lambda _: False),
        _transcription_changed=lambda: None,
    )
    controller = VoiceBackfill(
        app, later=lambda f: timers.append(f) or 1, post=lambda f, *a: posted.append((f, a)), cancel=lambda _: None
    )
    meeting = S(
        directory=Path("old"),
        lifecycle="saved",
        transcript="done",
        metadata_editable=True,
        has_audio=True,
        speakers=(S(id="others"),),
        words=(S(speaker="others"),),
    )
    try:
        controller.observe(S(unavailable=False, meetings=(meeting,)))
        for preempted in (True, False):
            timers.pop(0)()
            until(lambda: bool(posted))
            f, args = posted.pop(0)
            f(*args)
            assert controller.running
            callbacks.pop()(meeting.directory, preempted)
            f, args = posted.pop(0)
            f(*args)
        assert not timers
        controller.observe(S(unavailable=False, meetings=(meeting,)))
        assert not timers
    finally:
        controller.close()
