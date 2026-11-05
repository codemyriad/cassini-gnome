# SPDX-License-Identifier: GPL-3.0-or-later
"""A silent broker's last capture state expires everywhere: Library, Record and Quit."""

import socket
from concurrent.futures import Future, ThreadPoolExecutor
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_record_toggle import app_with, pump, window
from test_voice_application import application_class  # noqa: F401

pytest.importorskip("gi")


@pytest.fixture
def broker(application_class, monkeypatch):  # noqa: F811
    """The real BrowserWatch (the app's snapshot, copied to each window) and a
    Library window's real BrowserActivity, both seeded with a live recording
    answered at 0 and stamped on their workers by one injected monotonic clock.
    Each poll takes the next scripted answer; the test waits on its completion."""
    from cassini_talk import browser_service, browser_watch, capture_activity
    from cassini_talk.capture_activity import application_activity
    from cassini_talk.capture_guard import capture_targets

    identifier = str(uuid4())
    path = "/library/Recording-" + identifier
    status = dict(id="capture:" + identifier, recording=True, phase="recording", path=path)
    live_watch = dict(statuses=[status], meetings=[], recordings=[], autoRecord=False, autoRecordRevision=0)
    live_activity = ({identifier: "recording"}, True, {identifier: path})
    silent = object()
    now, watch_replies, activity_replies = [0.0], [live_watch], [live_activity]
    # The real read, for polls that go to a real socket.
    read_activity = capture_activity.read_browser_activity

    def watch_read(_watch):
        reply = watch_replies.pop(0)
        if reply is silent:
            raise TimeoutError("broker did not answer")
        return reply

    def activity_read(*args, **kwargs):
        reply = activity_replies.pop(0)
        return reply(*args, **kwargs) if callable(reply) else reply

    monkeypatch.setattr(browser_watch.BrowserWatch, "_read", watch_read)
    monkeypatch.setattr(capture_activity, "read_browser_activity", activity_read)

    library = window()
    app, events = app_with(application_class, windows=[library])
    executor = ThreadPoolExecutor(max_workers=1)
    app.__dict__.update(
        _browser_answered_at=None,
        _browser_expiry=0,
        _broker_clock=lambda: now[0],
        _auto_recording_revision=0,
        _auto_recording_pending=None,
        _permission_sources=set(),
        _browser_seen_saved=set(),
        _record_start_generation=0,
        _record_start_group=None,
        _record_start_executor=executor,
        _quitting=False,
        _capture_exiting=False,
        _pending_recording=None,
        _show_pending_recording=lambda: None,
        browser_companion_available=True,
        recording_failures=[],
        recording_error=None,
        browser_connection=None,
        transcription_enabled=False,
        set_auto_recording_hold=lambda _enabled: None,
        discard_test_recording=lambda: None,
        hold=lambda: None,
        release=lambda: None,
        start_native=lambda: pytest.fail("A browser meeting records: no native start"),
    )
    for name in (
        "_browser_changed",
        "_browser_unanswered",
        "_stop_browser_watch",
        "_expire_unwatched",
        "_unwatched_expiry",
        "finish_auto_recording_change",
        "browser_capture_active",
        "recording_active",
        "observe_browser_recordings",
        "start_recordings",
        "_recordings_started",
        "_prune_recording_failures",
    ):
        if hasattr(application_class, name):
            setattr(app, name, getattr(application_class, name).__get__(app))
    # What the Library publishes on each refresh either cache triggers.
    published = []
    app._native_changed = lambda *_: published.append(application_activity(app, library._activity_source))

    activity = capture_activity.BrowserActivity(
        lambda: published.append(application_activity(app, activity)), interval_ms=3_600_000, clock=lambda: now[0]
    )
    library._activity_source = activity
    watch = app.browser_watch = browser_watch.BrowserWatch(
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

    def record(at, reply):
        """Record's start-all, answered (and stamped on its worker) at `at`."""
        now[0] = at
        monkeypatch.setattr(browser_service, "ensure_service", lambda: "/broker.sock")
        monkeypatch.setattr(browser_service, "request", lambda *_args, **_kwargs: reply)
        app.start_recordings()
        pump(lambda: app._record_start_future is None, "start-all")

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

    harness = SimpleNamespace(
        app=app,
        library=library,
        activity=activity,
        watch=watch,
        now=now,
        identifier=identifier,
        path=path,
        status=status,
        live_watch=live_watch,
        idle_watch=dict(live_watch, statuses=[]),
        live_activity=live_activity,
        idle_activity=({}, True, {}),
        silent=silent,
        read_activity=read_activity,
        published=published,
        watch_replies=watch_replies,
        activity_replies=activity_replies,
        poll=poll,
        record=record,
        readings=readings,
        recording=dict(
            library={identifier: "recording"},
            record=["stop"],
            record_stops=True,
            quit_busy=True,
            browser_active=True,
            recording_active=True,
        ),
        idle=dict(
            library={},
            record=["activate", "record"],
            record_stops=False,
            quit_busy=False,
            browser_active=False,
            recording_active=False,
        ),
    )
    try:
        settle()
        assert app._browser_answered_at == 0 and activity.answered_at == 0
        assert library._browser_snapshot["statuses"] == [status]
        assert readings() == harness.recording
        yield harness
    finally:
        watch.close()
        activity.close()
        executor.shutdown(wait=True)
        library._activity_source = SimpleNamespace(snapshot={}, connected=False)


def test_a_silent_broker_reads_idle_in_library_record_and_quit_and_the_newest_reply_wins(broker):
    """Both caches of the broker's capture state, the app's (BrowserWatch, copied
    to each window) and the Library's (BrowserActivity), keep the last answer
    while polls go unanswered, only up to UNANSWERED_BOUND from that answer.
    Seeding only one of them hid the other: the app's snapshot kept a frozen
    broker's meeting "recording", Record a Stop and Quit busy at 100 s."""
    from cassini_talk.capture_activity import UNANSWERED, UNANSWERED_BOUND

    b, app, silent = broker, broker.app, broker.silent
    # A late broker has not said that nothing records: both caches keep it.
    b.poll(1, silent, UNANSWERED)
    b.poll(UNANSWERED_BOUND - 0.1, silent, UNANSWERED)
    assert b.readings() == b.recording, "Within the bound the last answer stands"

    # A poll failing at the bound from the last answer: idle everywhere, published.
    b.published.clear()
    b.poll(UNANSWERED_BOUND, silent, UNANSWERED)
    assert b.readings() == b.idle
    assert b.library._browser_snapshot["statuses"] == [] and app._browser_snapshot["statuses"] == []
    assert b.activity.snapshot == {}
    assert b.published and b.published[-1] == {}, "The expiry must reach the Library, not wait for a read"
    b.poll(100, silent, UNANSWERED)
    assert b.readings() == b.idle, "Still idle at 100 s"

    # A fresh answer restores the capture everywhere.
    b.poll(101, b.live_watch, b.live_activity)
    assert b.readings() == b.recording
    assert b.published[-1] == {b.identifier: "recording"}

    # The newest answer wins across caches: the Library's poll answered
    # after the app's says nothing records, then the app's newer one says it does.
    b.poll(102, activity_reply=b.idle_activity)
    assert app._browser_snapshot["statuses"] == [b.status], "The app's older answer is still held..."
    assert b.readings() == b.idle, "...but the Library's newer answer wins"
    b.poll(103, watch_reply=b.live_watch)
    assert b.readings() == b.recording

    # An answer received earlier but delivered later never wins: a Settings
    # configure reply received at 103.5 lands after Watch's idle answer of 104.
    b.poll(104, watch_reply=b.idle_watch)
    assert b.readings() == b.idle
    app._auto_recording_pending = (7, True)
    app.finish_auto_recording_change(7, dict(b.live_watch, autoRecord=True, autoRecordRevision=1), 103.5)
    assert app._browser_answered_at == 104 and b.readings() == b.idle
    assert app._browser_snapshot["autoRecord"] is True, "Its preference still lands, ordered by its revision"
    # Nor does a Library answer received before the one it holds.
    older = Future()
    older.set_result((101.5, b.live_activity))
    b.activity._complete(older)
    assert b.activity.answered_at == 102 and b.readings() == b.idle
    assert not b.watch_replies and not b.activity_replies


def test_a_library_poll_that_fails_is_not_an_answer_that_nothing_records(broker, tmp_path):
    """A stalled broker (still listening, its accept loop stuck) fills its
    backlog: the Library's next connect fails at once (EAGAIN), well within the
    bound. That failure is no reply. Stamped as an answer it was the newest one,
    so Watch's live reply stopped counting and the meeting read idle everywhere."""
    from cassini_talk.capture_activity import UNANSWERED_BOUND

    b = broker
    read_browser_activity = b.read_activity
    stalled = tmp_path / "stalled.sock"
    with socket.socket(socket.AF_UNIX) as server, socket.socket(socket.AF_UNIX) as queued:
        server.bind(str(stalled))
        server.listen(0)
        queued.settimeout(0.25)
        queued.connect(str(stalled))  # The backlog's one slot; nobody accepts it.
        b.poll(5, watch_reply=b.live_watch)
        b.activity.path = stalled
        b.poll(6, activity_reply=read_browser_activity)
        assert b.readings() == b.recording, "A full backlog is a late broker, not an idle one"
    # The broker's socket gone (ENOENT) is no answer either: it is a broker that
    # stopped answering, bounded like any other.
    b.activity.path = tmp_path / "gone.sock"
    b.poll(7, activity_reply=read_browser_activity)
    assert b.readings() == b.recording
    assert b.activity.answered_at == 0 and b.app._browser_answered_at == 5
    # Silent for the bound from Watch's last reply: idle everywhere, as before.
    b.poll(5 + UNANSWERED_BOUND, b.silent, read_browser_activity)
    assert b.readings() == b.idle


def test_records_own_start_reply_is_a_stamped_answer(broker):
    """Record's start-all reply saw the meeting start: it is the newest answer.
    Unstamped, the very next failed Watch poll expired it against Watch's old
    reply, and a Library answer newer than Watch's hid it at once: Record was no
    Stop (a second press started another recording) and Quit was not busy."""
    from cassini_talk.capture_activity import UNANSWERED, UNANSWERED_BOUND

    b = broker
    started = dict(b.live_watch, attempted=1)
    # A busy broker: status polls time out until both caches expire at the bound.
    b.poll(1, b.silent, UNANSWERED)
    b.poll(UNANSWERED_BOUND, b.silent, UNANSWERED)
    assert b.readings() == b.idle
    # Record: start-all (30 s timeout) gets through and says the meeting records.
    b.record(20, started)
    assert b.readings() == b.recording
    b.poll(20.5, b.silent, UNANSWERED)
    assert b.readings() == b.recording, "The bound runs from start-all's reply, not Watch's last one"
    assert b.app._browser_answered_at == 20
    b.poll(20 + UNANSWERED_BOUND - 0.1, b.silent, UNANSWERED)
    assert b.readings() == b.recording
    b.poll(20 + UNANSWERED_BOUND, b.silent, UNANSWERED)
    assert b.readings() == b.idle

    # Both caches answer idle, the Library's last; then Record's reply is newer still.
    b.poll(39.6, watch_reply=b.idle_watch)
    b.poll(39.9, activity_reply=b.idle_activity)
    b.record(40, started)
    assert b.readings() == b.recording, "Record's reply is newer than the Library's"
    # A Watch reply received before start-all's but delivered after it never wins.
    b.poll(39.95, watch_reply=b.idle_watch)
    assert b.app._browser_answered_at == 40 and b.readings() == b.recording
    assert not b.watch_replies and not b.activity_replies


def test_a_saving_capture_reads_the_same_whichever_cache_answered_last(broker):
    """Both caches project one broker state the same way. The app's snapshot
    counted only "recording" for browser_capture_active while BrowserActivity
    counted its "saving" entries too, so under newest-wins a saving capture
    flipped it (and the suspend inhibit) on every poll."""
    b = broker
    saving = dict(b.live_watch, statuses=[dict(b.status, recording=False, stopping=True, phase="stopping")])
    saving_activity = ({b.identifier: "saving"}, True, {b.identifier: b.path})
    expected = dict(b.idle, library={b.identifier: "saving"}, quit_busy=True)
    seen = []
    for at, watch_reply, activity_reply in (
        (1.0, saving, None),
        (1.3, None, saving_activity),
        (1.5, saving, None),
        (2.3, None, saving_activity),
    ):
        b.poll(at, watch_reply, activity_reply)
        seen.append(b.readings())
    assert seen == [expected] * 4


def test_a_closed_watch_still_expires_the_apps_snapshot_at_the_bound(broker):
    """When the companion goes unavailable the app closes BrowserWatch: no poll
    follows, so none fails, and the snapshot's "recording" stood forever (Codex's
    100 s reading). It now expires at the same bound from its last reply."""
    from cassini_talk.capture_activity import UNANSWERED, UNANSWERED_BOUND

    b, app = broker, broker.app
    b.now[0] = UNANSWERED_BOUND - 0.05
    app._stop_browser_watch()
    assert app.browser_watch is None and b.watch.closed.is_set()
    assert b.readings() == b.recording, "Closing the watch is not an answer that nothing records"
    b.poll(UNANSWERED_BOUND - 0.02, activity_reply=UNANSWERED)
    assert b.readings() == b.recording
    b.published.clear()
    b.poll(UNANSWERED_BOUND, activity_reply=UNANSWERED)
    pump(lambda: not app._browser_snapshot.get("statuses"), "the closed watch's snapshot expiry")
    assert b.library._browser_snapshot["statuses"] == []
    assert b.readings() == b.idle
    assert b.published and b.published[-1] == {}
    b.poll(100, activity_reply=UNANSWERED)
    assert b.readings() == b.idle, "Still idle at 100 s"
