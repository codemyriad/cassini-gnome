# SPDX-License-Identifier: GPL-3.0-or-later
"""A silent broker's last capture state expires everywhere: Library, Record and Quit."""

from concurrent.futures import Future
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_record_toggle import app_with, pump, window
from test_voice_application import application_class  # noqa: F401

pytest.importorskip("gi")


def test_a_silent_broker_reads_idle_in_library_record_and_quit_and_the_newest_reply_wins(
    application_class,  # noqa: F811
    monkeypatch,
):
    """Both caches of the broker's capture state, the app's (BrowserWatch, copied
    to each window) and the Library's (BrowserActivity), keep the last answer
    while polls go unanswered, only up to UNANSWERED_BOUND from that answer.
    Seeding only one of them hid the other: the app's snapshot kept a frozen
    broker's meeting "recording", Record a Stop and Quit busy at 100 s."""
    from cassini_talk import browser_watch, capture_activity
    from cassini_talk.capture_activity import UNANSWERED, UNANSWERED_BOUND, application_activity
    from cassini_talk.capture_guard import capture_targets

    identifier = str(uuid4())
    path = "/library/Recording-" + identifier
    status = dict(id="capture:" + identifier, recording=True, phase="recording", path=path)
    live_watch = dict(statuses=[status], meetings=[], recordings=[], autoRecord=False, autoRecordRevision=0)
    idle_watch = dict(live_watch, statuses=[])
    live_activity = ({identifier: "recording"}, True, {identifier: path})
    idle_activity = ({}, True, {})
    silent = object()

    # The real pollers; each poll takes the next scripted answer and is stamped
    # on its worker by the one injected monotonic clock.
    now, watch_replies, activity_replies = [0.0], [live_watch], [live_activity]

    def watch_read(_watch):
        reply = watch_replies.pop(0)
        if reply is silent:
            raise TimeoutError("broker did not answer")
        return reply

    monkeypatch.setattr(browser_watch.BrowserWatch, "_read", watch_read)
    monkeypatch.setattr(capture_activity, "read_browser_activity", lambda *_, **__: activity_replies.pop(0))

    library = window()
    app, events = app_with(application_class, windows=[library])
    app.__dict__.update(
        _browser_answered_at=None,
        _auto_recording_revision=0,
        _auto_recording_pending=None,
        _permission_sources=set(),
        _browser_seen_saved=set(),
        browser_connection=None,
        transcription_enabled=False,
        set_auto_recording_hold=lambda _enabled: None,
        release=lambda: None,
    )
    for name in (
        "_browser_changed",
        "_browser_unanswered",
        "finish_auto_recording_change",
        "browser_capture_active",
        "recording_active",
        "observe_browser_recordings",
    ):
        setattr(app, name, getattr(application_class, name).__get__(app))
    # What the Library publishes on each refresh either cache triggers.
    published = []
    app._native_changed = lambda *_: published.append(application_activity(app, library._activity_source))

    activity = capture_activity.BrowserActivity(
        lambda: published.append(application_activity(app, activity)), interval_ms=3_600_000, clock=lambda: now[0]
    )
    library._activity_source = activity
    watch = browser_watch.BrowserWatch(
        app._browser_changed, unanswered=app._browser_unanswered, interval_ms=3_600_000, clock=lambda: now[0]
    )

    def settle():
        pump(lambda: watch.future is None and activity.future is None, "broker polls")

    def poll(at, watch_reply=None, activity_reply=None):
        now[0] = at
        if watch_reply is not None:
            watch_replies.append(watch_reply)
            watch.poll()
        if activity_reply is not None:
            activity_replies.append(activity_reply)
            activity.poll()
        settle()

    def readings():
        """What Library, Record and Quit read now."""
        events.clear()
        app.on_record()
        return dict(
            library=application_activity(app, activity),
            record=events[:],
            record_stops=bool(app.recording_to_stop()),
            quit_busy=capture_targets(app)[1],
            browser_active=app.browser_capture_active(),
            recording_active=app.recording_active(),
        )

    recording = dict(
        library={identifier: "recording"},
        record=["stop"],
        record_stops=True,
        quit_busy=True,
        browser_active=True,
        recording_active=True,
    )
    idle = dict(
        library={},
        record=["activate", "record"],
        record_stops=False,
        quit_busy=False,
        browser_active=False,
        recording_active=False,
    )

    try:
        settle()
        assert app._browser_answered_at == 0 and activity.answered_at == 0
        assert library._browser_snapshot["statuses"] == [status]
        assert readings() == recording

        # A late broker has not said that nothing records: both caches keep it.
        poll(1, silent, UNANSWERED)
        poll(UNANSWERED_BOUND - 0.1, silent, UNANSWERED)
        assert readings() == recording, "Within the bound the last answer stands"

        # A poll failing at the bound from the last answer: idle everywhere, published.
        published.clear()
        poll(UNANSWERED_BOUND, silent, UNANSWERED)
        assert readings() == idle
        assert library._browser_snapshot["statuses"] == [] and app._browser_snapshot["statuses"] == []
        assert activity.snapshot == {}
        assert published and published[-1] == {}, "The expiry must reach the Library, not wait for a read"
        poll(100, silent, UNANSWERED)
        assert readings() == idle, "Still idle at 100 s"

        # A fresh answer restores the capture everywhere.
        poll(101, live_watch, live_activity)
        assert readings() == recording
        assert published[-1] == {identifier: "recording"}

        # The newest answer wins across caches: the Library's poll answered
        # after the app's says nothing records, then the app's newer one says it does.
        poll(102, activity_reply=idle_activity)
        assert app._browser_snapshot["statuses"] == [status], "The app's older answer is still held..."
        assert readings() == idle, "...but the Library's newer answer wins"
        poll(103, watch_reply=live_watch)
        assert readings() == recording

        # An answer received earlier but delivered later never wins: a Settings
        # configure reply received at 103.5 lands after Watch's idle answer of 104.
        poll(104, watch_reply=idle_watch)
        assert readings() == idle
        app._auto_recording_pending = (7, True)
        app.finish_auto_recording_change(7, dict(live_watch, autoRecord=True, autoRecordRevision=1), 103.5)
        assert app._browser_answered_at == 104 and readings() == idle
        assert app._browser_snapshot["autoRecord"] is True, "Its preference still lands, ordered by its revision"
        # Nor does a Library answer received before the one it holds.
        older = Future()
        older.set_result((101.5, live_activity))
        activity._complete(older)
        assert activity.answered_at == 102 and readings() == idle
        assert not watch_replies and not activity_replies
    finally:
        watch.close()
        activity.close()
        library._activity_source = SimpleNamespace(snapshot={}, connected=False)
