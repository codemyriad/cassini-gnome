# SPDX-License-Identifier: GPL-3.0-or-later
"""Private broker/controller state; never infer recording from file timestamps."""

from types import SimpleNamespace

import pytest

pytest.importorskip("gi")
from test_browser_service import browser, service, wait_for  # noqa: E402, F401
from test_library import capture  # noqa: E402
from test_library_monitor import spin_until  # noqa: E402
from test_voice_application import application_class  # noqa: E402, F401

from cassini_talk.browser_service import send_packet  # noqa: E402
from cassini_talk.capture_activity import BrowserActivity, read_browser_activity, recorder_activity  # noqa: E402


def pump(condition):
    """Iterate the main context until condition holds; 5 s only bounds it (E1)."""
    from gi.repository import GLib

    expired = []
    source = GLib.timeout_add_seconds(5, lambda: expired.append(True) or GLib.SOURCE_REMOVE)
    try:
        while not condition():
            assert not expired, "activity poll did not complete"
            GLib.MainContext.default().iteration(True)
    finally:
        if not expired:
            GLib.source_remove(source)


def test_real_broker_reports_canonical_recording_and_clears_on_stop(service, tmp_path):  # noqa: F811
    _, path = service
    session = capture(tmp_path)
    connection, stream = browser(path)
    changes = []
    watcher = BrowserActivity(lambda: changes.append(dict(watcher.snapshot)), path=path, interval_ms=10)
    try:
        send_packet(stream, dict(type="status", recording=True, path=str(session.directory)))
        spin_until(lambda: watcher.snapshot == {session.manifest["id"]: "recording"})
        assert read_browser_activity(path) == watcher.snapshot
        assert watcher.paths == {session.manifest["id"]: str(session.directory)}
        send_packet(stream, dict(type="status", recording=False, path=str(session.directory)))
        spin_until(lambda: not watcher.snapshot and len(changes) >= 2)
        assert watcher.paths == {}
        send_packet(stream, dict(type="status", recording=True, path="/tmp/Recording-not-a-uuid"))
        wait_for(lambda: read_browser_activity(path) == {})
    finally:
        watcher.close()
        stream.close()
        connection.close()


def test_browser_watch_delivers_profile_changes_once_off_the_worker(service, monkeypatch):  # noqa: F811
    import threading

    from cassini_talk import browser_watch, companion

    now, profile = [0], ["profile-one"]
    probes, notifications = [], []
    main = threading.get_ident()

    def inspect():
        probes.append(threading.get_ident())
        return profile[0]

    monkeypatch.setattr(companion, "profile_inventory", inspect)
    monkeypatch.setattr(
        browser_watch, "time", SimpleNamespace(monotonic=lambda: now[0], monotonic_ns=browser_watch.time.monotonic_ns)
    )
    watcher = browser_watch.BrowserWatch(
        lambda *_: None, path=service[1], profiles_changed=lambda: notifications.append(threading.get_ident())
    )
    try:
        spin_until(lambda: watcher.future is None)
        assert notifications == [main]
        for second, name, count in ((6, "profile-one", 1), (12, "profile-two", 2)):
            now[0], profile[0] = second, name
            watcher.poll()
            spin_until(lambda: watcher.future is None)
            assert len(notifications) == count
        assert len(probes) == 3 and all(t != main for t in probes)
    finally:
        watcher.close()


def test_slow_broker_is_bounded_and_does_not_block_main_context(tmp_path):
    import socket
    import threading

    from gi.repository import GLib

    path = tmp_path / "unresponsive.sock"
    accepted, release = threading.Event(), threading.Event()
    with socket.socket(socket.AF_UNIX) as server:
        server.bind(str(path))
        server.listen()

        def serve():
            with server.accept()[0]:
                accepted.set()
                release.wait(2)

        thread = threading.Thread(target=serve)
        thread.start()
        watcher = BrowserActivity(lambda: None, path=path, interval_ms=10000)
        try:
            spin_until(accepted.is_set)
            heartbeat = []
            GLib.idle_add(lambda: heartbeat.append(True) and False)
            spin_until(lambda: bool(heartbeat))
            spin_until(lambda: watcher.future is None, timeout=1)
            assert watcher.snapshot == {}
        finally:
            watcher.close()
            release.set()
            thread.join(2)
    assert not read_browser_activity(tmp_path / "absent.sock")


def test_an_unanswered_poll_keeps_the_live_capture_and_a_real_reply_clears_it(tmp_path):
    """A broker that is late (its session checkpoint fsyncs under the capture
    lock that a status reply takes) has not said that nothing records. Treating
    the timeout as idle dropped the live meeting for one poll, which unbound its
    spectrogram and closed the meter subscription (the spectrogram-browser flake)."""
    import queue
    import socket
    import threading

    from cassini_talk.browser_service import read_packet

    session = capture(tmp_path, completed=False)
    identifier, directory = session.manifest["id"], str(session.directory)
    path = tmp_path / "late.sock"
    replies, polled, release = queue.Queue(), threading.Event(), threading.Event()
    live = dict(statuses=[dict(recording=True, path=directory)], connectedBrowsers=1)

    def serve(server):
        while (answer := replies.get()) is not None:
            connection = server.accept()[0]
            with connection, connection.makefile("rwb") as stream:
                assert read_packet(stream) == dict(role="app", action="status")
                if answer == "late":
                    polled.set()
                    release.wait()  # Never answers: the client's own bound ends the poll.
                else:
                    send_packet(stream, answer)

    changes = []
    with socket.socket(socket.AF_UNIX) as server:
        server.bind(str(path))
        server.listen()
        thread = threading.Thread(target=serve, args=(server,))
        thread.start()
        replies.put(live)
        watcher = BrowserActivity(lambda: changes.append(dict(watcher.snapshot)), path=path, interval_ms=3_600_000)
        try:
            pump(lambda: watcher.future is None)
            assert changes == [{identifier: "recording"}] and watcher.connected

            replies.put("late")
            watcher.poll()
            pump(lambda: polled.is_set() and watcher.future is None)
            assert watcher.snapshot == {identifier: "recording"}, "A late reply is unknown, not idle"
            assert watcher.paths == {identifier: directory} and watcher.connected
            assert len(changes) == 1, "A late reply must not publish a change"

            release.set()
            replies.put(dict(statuses=[], connectedBrowsers=1))
            watcher.poll()
            pump(lambda: watcher.future is None)
            assert watcher.snapshot == {} and watcher.paths == {}
            assert changes == [{identifier: "recording"}, {}]
        finally:
            watcher.close()
            release.set()
            replies.put(None)
            thread.join(5)
    assert not thread.is_alive()
    # Without a broker, and nothing answered yet, it still reads as idle.
    assert read_browser_activity(path, True, True) == ({}, False, {})


def test_unanswered_polls_keep_the_live_capture_only_up_to_the_bound(tmp_path, monkeypatch):
    """A broker that is late is unknown, not idle, but a frozen broker that still
    holds its socket must not keep a meeting "recording" forever: once a poll
    fails UNANSWERED_BOUND after the last answer the Library reads it as idle, once."""
    from concurrent.futures import Future

    from cassini_talk import capture_activity
    from cassini_talk.capture_activity import UNANSWERED, UNANSWERED_BOUND

    session = capture(tmp_path, completed=False)
    identifier, directory = session.manifest["id"], str(session.directory)
    live = ({identifier: "recording"}, True, {identifier: directory})
    replies, now, changes = [live], [0.0], []
    monkeypatch.setattr(capture_activity, "read_browser_activity", lambda *_args, **_kwargs: replies.pop(0))
    watcher = BrowserActivity(
        lambda: changes.append(dict(watcher.snapshot)), interval_ms=3_600_000, clock=lambda: now[0]
    )

    def poll(result, at):
        now[0] = at
        replies.append(result)
        watcher.poll()
        pump(lambda: watcher.future is None)

    def holds_live():
        return (watcher.snapshot, watcher.connected, watcher.paths) == live

    try:
        pump(lambda: watcher.future is None)
        assert holds_live() and changes == [{identifier: "recording"}] and watcher.answered_at == 0

        # Before the bound the last answer stands and nothing is published.
        poll(UNANSWERED, 1)
        poll(UNANSWERED, UNANSWERED_BOUND - 0.1)
        assert holds_live() and len(changes) == 1
        # A poll failing at the bound after the last answer reads as idle, published once.
        poll(UNANSWERED, UNANSWERED_BOUND)
        assert (watcher.snapshot, watcher.connected, watcher.paths) == ({}, False, {})
        assert changes == [{identifier: "recording"}, {}]
        poll(UNANSWERED, 1 + UNANSWERED_BOUND)
        assert len(changes) == 2, "Further unanswered polls publish no more changes"
        # A real reply after that restores the capture.
        poll(live, 13)
        assert holds_live() and changes[-1] == {identifier: "recording"} and len(changes) == 3

        # Each answer restarts the bound: silence is timed from the last answer.
        poll(UNANSWERED, 14)
        poll(UNANSWERED, 13 + UNANSWERED_BOUND - 0.1)
        poll(live, 13 + UNANSWERED_BOUND - 0.05)
        poll(UNANSWERED, 30)
        poll(UNANSWERED, 13 + 2 * UNANSWERED_BOUND - 0.15)
        assert holds_live() and len(changes) == 3, "An answer must restart the bound"

        # A reply that nothing records is idle at once; a failure is no reply.
        poll(({}, True, {}), 41)
        assert watcher.answered_at == 41 and changes[3:] == [{}]
        poll(live, 42)
        assert holds_live() and changes[3:] == [{}, {identifier: "recording"}]
        assert not replies

        # An answer received before the one held never replaces it, whenever it lands.
        older = Future()
        older.set_result((41.5, ({}, False, {}), {}))
        watcher._complete(older)
        assert holds_live() and watcher.answered_at == 42 and len(changes) == 5
    finally:
        watcher.close()


def test_only_a_brokers_reply_or_its_absence_is_an_answer(tmp_path):
    """With `unanswered`, a status reply the broker sent is an answer, and so
    is no broker at all (no socket, or one nothing listens on): idle, nothing
    records. A broker that is there but did not answer (a stalled broker's full
    backlog, an EOF before the reply, a malformed or error reply) returns
    `unanswered`: BrowserActivity keeps its last answer up to the bound
    (broker_silent), as BrowserWatch does for the same failures, instead of a
    newest "idle"."""
    import socket
    import threading

    from cassini_talk.browser_service import read_packet
    from cassini_talk.capture_activity import UNANSWERED

    def read(path):
        return read_browser_activity(path, True, True, unanswered=UNANSWERED)

    assert read(tmp_path / "absent.sock") == ({}, False, {}), "No socket: no broker, nothing records"
    with socket.socket(socket.AF_UNIX) as refused:
        refused.bind(str(tmp_path / "refused.sock"))  # Bound, never listening.
        assert read(tmp_path / "refused.sock") == ({}, False, {}), "Refused: no broker listens"
    with socket.socket(socket.AF_UNIX) as stalled, socket.socket(socket.AF_UNIX) as queued:
        stalled.bind(str(tmp_path / "stalled.sock"))
        stalled.listen(0)
        queued.settimeout(0.25)
        queued.connect(str(tmp_path / "stalled.sock"))  # Fills the backlog; nobody accepts.
        assert read(tmp_path / "stalled.sock") is UNANSWERED

    path = tmp_path / "broker.sock"
    partial = b'{"statuses": ['  # Cut short: no newline, then EOF.
    replies = [None, dict(error="Browser inventory unavailable"), partial, dict(statuses=[], connectedBrowsers=1)]
    with socket.socket(socket.AF_UNIX) as server:
        server.bind(str(path))
        server.listen()

        def serve():
            for reply in replies:
                connection = server.accept()[0]
                with connection, connection.makefile("rwb") as stream:
                    read_packet(stream)
                    if isinstance(reply, bytes):
                        stream.write(reply)
                    elif reply is not None:
                        send_packet(stream, reply)

        thread = threading.Thread(target=serve)
        thread.start()
        try:
            assert read(path) is UNANSWERED, "EOF before a reply is no answer"
            assert read(path) is UNANSWERED, "An error reply is no answer"
            assert read(path) is UNANSWERED, "A partial reply is no answer"
            assert read(path) == ({}, True, {}), "A reply that nothing records is one"
        finally:
            thread.join(5)
    assert not thread.is_alive()
    # Callers without the sentinel (the relocator) still read a failure as idle.
    assert read_browser_activity(tmp_path / "absent.sock", True, True) == ({}, False, {})


def test_native_identity_only_comes_from_controller_owned_session(tmp_path):
    session = capture(tmp_path)
    recorder = SimpleNamespace(pipeline=object(), session=session, stopping=False)
    window = SimpleNamespace(recorder=recorder)
    assert recorder_activity([window]) == {session.manifest["id"]: "recording"}
    recorder.stopping = True
    assert recorder_activity([window]) == {session.manifest["id"]: "saving"}
    recorder.pipeline = None
    assert recorder_activity([window]) == {session.manifest["id"]: "saving"}
    recorder.stopping = False
    recorder._stop_requested = True
    assert recorder_activity([window]) == {session.manifest["id"]: "saving"}
    recorder._stop_requested = False
    assert recorder_activity([window]) == {}
    standalone = SimpleNamespace(
        recorder=SimpleNamespace(pipeline=object(), path=str(tmp_path / "cassini-2026-10-27.opus"), stopping=False)
    )
    assert not recorder_activity([standalone]), "Standalone audio must never acquire an invented Library UUID"

    from cassini_talk.capture_guard import capture_saving_only

    second = capture(tmp_path)
    app = SimpleNamespace(
        native_captures={"one": SimpleNamespace(pipeline=object(), session=session, stopping=False)},
        _browser_snapshot=dict(statuses=[dict(recording=True, stopping=False, path=str(second.directory))]),
        _browser_stops=set(),
        _browser_capture_phases={},
        get_windows=lambda: [],
    )
    assert not capture_saving_only(app)
    app.native_captures["one"]._stop_requested = True
    assert not capture_saving_only(app), "Second browser capture still records"
    app._browser_stops.add(str(second.directory))
    assert capture_saving_only(app), "All active captures are now saving"


@pytest.mark.parametrize("failure", ["timeout", "exit"])
def test_desktop_probe_failure_preserves_watch_lease_and_status_polling(service, tmp_path, monkeypatch, failure):  # noqa: F811
    import subprocess

    from cassini_talk import browser_watch, companion, flatpak_bridge

    failed = [False]
    real_run = subprocess.run

    def run(command, **kwargs):
        if "cassini-desktops" in command:
            if failed[0]:
                if failure == "timeout":
                    raise subprocess.TimeoutExpired(command, 2)
                raise subprocess.CalledProcessError(1, command)
            return subprocess.CompletedProcess(command, 0, stdout=(str(tmp_path) + "\0").encode())
        return real_run(command, **kwargs)

    monkeypatch.setattr(companion, "in_flatpak", lambda: True)
    monkeypatch.setattr(
        flatpak_bridge, "deployment", lambda: flatpak_bridge.Deployment("io.codemyriad.CassiniTalk", "x86_64", "stable")
    )
    monkeypatch.setattr(
        flatpak_bridge,
        "host_info",
        lambda: (tmp_path / "config", tmp_path / "data", "/usr/bin/flatpak", {"chromium": "/host/bin/chromium"}),
    )
    monkeypatch.setattr(flatpak_bridge, "host_command", lambda command, **_: command)
    monkeypatch.setattr(flatpak_bridge.subprocess, "run", run)
    statuses, profiles = [], []
    watcher = browser_watch.BrowserWatch(
        lambda value, _at: statuses.append(value),
        path=service[1],
        profiles_changed=lambda: profiles.append(watcher._profiles),
    )
    try:
        spin_until(lambda: watcher.future is None and watcher.lease is not None)
        lease = watcher.lease
        delivered = len(statuses)
        failed[0] = True
        watcher._profile_checked = float("-inf")
        watcher.poll()
        spin_until(lambda: watcher.future is None)
        assert watcher.lease is lease and lease.fileno() >= 0, "A profile probe must not drop the application lease"
        assert len(statuses) > delivered, "Broker status polling must continue after the probe fails"
        assert profiles[-1] == "unavailable"
    finally:
        watcher.close()


def test_terminal_capture_phases_survive_cache_replacement_without_retiring_a_new_session():
    from uuid import uuid4

    from cassini_talk.capture_activity import advance_capture_snapshot, retired_captures

    old, new = str(uuid4()), str(uuid4())
    app = SimpleNamespace(get_windows=lambda: [])

    def reply(identifier, **state):
        return dict(statuses=[dict(path="/tmp/Recording-" + identifier, phase="recording", **state)])

    saving = advance_capture_snapshot(app, reply(old, recording=False, stopping=True))
    assert saving["statuses"][0]["stopping"] is True
    delayed = advance_capture_snapshot(app, reply(old, recording=True, stopping=False))
    assert delayed["statuses"][0]["phase"] == "stopping"
    advance_capture_snapshot(app, reply(old, recording=False, stopping=False))
    advance_capture_snapshot(app, dict(statuses=[]))
    stale_saving = advance_capture_snapshot(app, reply(old, recording=False, stopping=True))
    assert stale_saving["statuses"][0]["stopping"] is False
    assert stale_saving["statuses"][0]["phase"] is None
    fresh = advance_capture_snapshot(app, reply(new, recording=True, stopping=False))
    assert fresh["statuses"][0]["recording"] is True
    assert retired_captures(app) == {old}


def test_broker_silence_does_not_establish_a_terminal_capture_phase():
    from uuid import uuid4

    from cassini_talk.capture_activity import advance_capture_snapshot

    app = SimpleNamespace(get_windows=lambda: [])
    status = dict(path="/tmp/Recording-" + str(uuid4()), recording=True, stopping=False)
    advance_capture_snapshot(app, dict(statuses=[status]))
    advance_capture_snapshot(app, dict(statuses=[]))
    answered_again = advance_capture_snapshot(app, dict(statuses=[status]))
    assert answered_again["statuses"][0]["recording"] is True


def test_an_older_activity_callback_still_advances_a_terminal_capture_fact():
    from concurrent.futures import Future
    from uuid import uuid4

    identifier = str(uuid4())
    changes = []
    activity = BrowserActivity.__new__(BrowserActivity)
    activity.closed = False
    activity.future = None
    activity.answered_at = 2
    activity.snapshot = {identifier: "recording"}
    activity.paths = {identifier: "/tmp/Recording-" + identifier}
    activity.connected = True
    activity._browser_capture_phases = {}
    activity.changed = lambda: changes.append(dict(activity.snapshot))
    older = Future()
    older.set_result((1, ({}, False, {}), {identifier: "finished"}))
    activity._complete(older)
    assert activity.answered_at == 2 and activity.connected is True
    assert activity.snapshot == {} and activity.paths == {}
    assert activity._browser_capture_phases == {identifier: "finished"}
    assert changes == [{}]


@pytest.mark.parametrize("targeted", [True, False])
def test_browser_stop_projects_saving_on_click_frame_and_retires_stale_poll_on_delivery(
    application_class,  # noqa: F811
    tmp_path,
    monkeypatch,
    targeted,
):
    """While a long browser recording muxes its .opus on a worker thread, Stop must
    project 'saving' synchronously on the click frame and retire stale 'recording'
    polls once Stop succeeds, without sticking in 'saving' if the Stop RPC fails."""
    import threading

    from gi.repository import GLib

    from cassini_talk import capture_guard
    from cassini_talk.capture_activity import application_activity
    from cassini_talk.library import LibraryScanner
    from cassini_talk.library_window import LibraryWindow

    session = capture(tmp_path, completed=False)
    identifier = session.manifest["id"]
    initial = LibraryScanner(tmp_path).scan(capture_activity={identifier: "recording"})
    assert initial.meetings[0].lifecycle == "recording"

    source = SimpleNamespace(
        snapshot={identifier: "recording"},
        paths={identifier: str(session.directory)},
        connected=True,
        answered_at=1.0,
        _browser_capture_phases={},
    )
    app = SimpleNamespace(
        _browser_snapshot=dict(statuses=[dict(recording=True, stopping=False, path=str(session.directory))]),
        _browser_answered_at=1.0,
        _browser_capture_phases={},
        _browser_stops=set(),
        _browser_stop_repeats={},
        _record_start_group=None,
        native_captures={},
        library_recovery=None,
        meeting_trash=SimpleNamespace(project=lambda snap: snap),
        observe_library=lambda *_args: None,
        transcription_enabled=False,
        recording_error=None,
        build_refresh=None,
        _notify_shortcut_saved=lambda _path: None,
    )
    scans, live_updates, projected = [], [], []
    window = SimpleNamespace(
        get_application=lambda: app,
        _activity_source=source,
        _fixture_activity={},
        _activity={identifier: "recording"},
        _source_snapshot=initial,
        snapshot=initial,
        monitor=SimpleNamespace(refresh_now=lambda: scans.append(True)),
        _update_live=lambda: live_updates.append(window.snapshot.meetings[0].lifecycle),
    )
    window._project_activity = LibraryWindow._project_activity.__get__(window)
    window._updated = lambda snap: (
        setattr(window, "snapshot", window._project_activity(snap))
        or projected.append(window.snapshot.meetings[0].lifecycle)
    )
    window.capture_changed = LibraryWindow.capture_changed.__get__(window)
    app.get_windows = lambda: [window]
    app._native_changed = window.capture_changed
    app.stop_browser_recordings = application_class.stop_browser_recordings.__get__(app)

    # 1. Failed Stop RPC projects 'saving' while in flight, then rolls back to 'recording'.
    fail_entered, release_fail = threading.Event(), threading.Event()
    fail_loop = GLib.MainLoop()
    fail_timed_out = []
    fail_timeout = GLib.timeout_add_seconds(5, lambda: fail_timed_out.append(True) or fail_loop.quit() or False)

    def failing_stop(*_args, **_kwargs):
        fail_entered.set()
        assert release_fail.wait(5)
        raise OSError("Broker socket busy")

    app.hold = lambda: None
    app.release = fail_loop.quit
    monkeypatch.setattr(capture_guard, "stop_browser_captures", failing_stop)
    try:
        app.stop_browser_recordings(session.directory if targeted else None)
        # Synchronously on the click frame (before worker finishes or main loop runs):
        assert window._activity == {identifier: "saving"}
        assert window.snapshot.meetings[0].lifecycle == "saving"
        assert live_updates[-1] == "saving"
        assert fail_entered.wait(5)
        release_fail.set()
        fail_loop.run()
        assert not fail_timed_out
        assert application_activity(app, source) == {identifier: "recording"}
    finally:
        release_fail.set()
        if not fail_timed_out:
            GLib.source_remove(fail_timeout)

    # 2. Successful Stop RPC submits its worker before UI projection, projects 'saving'
    # immediately, and keeps 'stopping'/'saving' until the broker confirms completion.
    import concurrent.futures

    mux_entered, release_mux = threading.Event(), threading.Event()
    ok_loop = GLib.MainLoop()
    ok_timed_out = []
    ok_timeout = GLib.timeout_add_seconds(5, lambda: ok_timed_out.append(True) or ok_loop.quit() or False)
    order = []
    real_executor = concurrent.futures.ThreadPoolExecutor

    class OrderedExecutor(real_executor):
        def submit(self, fn, /, *args, **kwargs):
            order.append("worker_submitted")
            return super().submit(fn, *args, **kwargs)

    window._updated = lambda snap: (
        order.append("ui_projected")
        or setattr(window, "snapshot", window._project_activity(snap))
        or projected.append(window.snapshot.meetings[0].lifecycle)
    )

    def muxing_stop(*_args, **_kwargs):
        mux_entered.set()
        assert release_mux.wait(5)
        return [str(session.directory)]

    app.release = ok_loop.quit
    monkeypatch.setattr(concurrent.futures, "ThreadPoolExecutor", OrderedExecutor)
    monkeypatch.setattr(capture_guard, "stop_browser_captures", muxing_stop)
    try:
        app.stop_browser_recordings(session.directory if targeted else None)
        assert order[:2] == ["worker_submitted", "ui_projected"]
        assert window._activity == {identifier: "saving"}
        assert window.snapshot.meetings[0].lifecycle == "saving"
        assert mux_entered.wait(5)
        release_mux.set()
        ok_loop.run()
        assert not ok_timed_out
        assert app._browser_capture_phases.get(identifier) == "stopping"
        assert application_activity(app, source) == {identifier: "saving"}
        assert capture_guard.capture_targets(app)[1] is True
        # Once the broker confirms publication finished, the session clears cleanly.
        app._browser_snapshot = dict(statuses=[dict(recording=False, stopping=False, path=str(session.directory))])
        source.snapshot = {}
        source._browser_capture_phases[identifier] = "finished"
        assert application_activity(app, source) == {}
        assert app._browser_capture_phases.get(identifier) == "finished"
        assert capture_guard.capture_targets(app) == ([], False)
    finally:
        release_mux.set()
        if not ok_timed_out:
            GLib.source_remove(ok_timeout)


def test_browser_stop_keeps_saving_and_close_guard_while_publication_runs_after_rpc(
    application_class,  # noqa: F811
    tmp_path,
    monkeypatch,
):
    """Managed browser captures return stoppedPaths when PCM stops while .opus
    publication continues in the background. Until the broker confirms completion,
    the Library must stay 'saving' and capture_targets() must stay active."""
    import threading
    import time
    from concurrent.futures import Future

    from broker_fixture import stop_broker
    from gi.repository import GLib
    from test_browser_service import SilentTap

    from cassini_talk import browser_service, capture_guard, library_publication
    from cassini_talk.browser_capture import BrowserCapture
    from cassini_talk.browser_service import BrowserService
    from cassini_talk.capture_activity import advance_capture_snapshot, application_activity
    from cassini_talk.library import LibraryScanner
    from cassini_talk.library_publication import capture_parent

    library_root = tmp_path / "Library"
    sessions_dir = capture_parent(library_root)
    sock_path = tmp_path / "private/browser.sock"
    publication_futures = []

    def gated_submit(session_obj, *, cancel=None):
        assert session_obj.finished and not cancel.is_set()
        future = Future()
        publication_futures.append(future)
        return future

    monkeypatch.setattr(library_publication, "submit_stopped", gated_submit)
    monkeypatch.setattr(browser_service, "service_path", lambda: sock_path)

    broker = BrowserService(
        tmp_path / "settings.json",
        command_timeout=0.3,
        notifier=lambda *_: lambda: None,
        capture_parent=sessions_dir,
        tap_factory=SilentTap,
    )
    ready = threading.Event()
    broker_thread = threading.Thread(target=broker.serve, args=(sock_path, ready), daemon=True)
    broker_thread.start()
    assert ready.wait(2)

    watcher = None
    try:
        managed = BrowserCapture(broker._capture_changed, parent=sessions_dir, tap_factory=SilentTap)
        managed.start(dict(type="start", captureId="pub-pending", timelineOriginMs=1_700_000_000_000, microphone=False))
        managed.handle(dict(type="track", id="remote-1", kind="remote", sampleRate=48000, channels=1))
        managed.handle(dict(type="audio", id="remote-1", sequence=0, offsetMs=0, pcm="AQACAA=="))
        with broker.lock:
            broker.captures[managed.capture_id] = managed
        identifier = managed.host.session.manifest["id"]

        watcher = BrowserActivity(lambda: None, path=sock_path, interval_ms=10_000)
        spin_until(lambda: watcher.future is None and watcher.snapshot == {identifier: "recording"})

        app = SimpleNamespace(
            _browser_snapshot=browser_service.request(),
            _browser_answered_at=time.monotonic(),
            _browser_capture_phases={},
            _browser_stops=set(),
            _browser_stop_repeats={},
            _record_start_group=None,
            _record_start_future=None,
            native_captures={},
            library_recovery=None,
            transcription_enabled=False,
            recording_error=None,
            build_refresh=None,
            _notify_shortcut_saved=lambda _path: None,
            _native_changed=lambda: None,
        )
        window = SimpleNamespace(_activity_source=watcher, _browser_snapshot=app._browser_snapshot)
        app.get_windows = lambda: [window]
        app.stop_browser_recordings = application_class.stop_browser_recordings.__get__(app)

        assert (
            LibraryScanner(library_root).scan(capture_activity=application_activity(app, watcher)).meetings[0].lifecycle
            == "recording"
        )
        assert capture_guard.capture_targets(app)[1] is True

        stop_loop = GLib.MainLoop()
        timed_out = []
        stop_timeout = GLib.timeout_add_seconds(5, lambda: timed_out.append(True) or stop_loop.quit() or False)
        app.hold = lambda: None
        app.release = stop_loop.quit
        try:
            app.stop_browser_recordings(managed.directory)
            stop_loop.run()
            assert not timed_out
        finally:
            if not timed_out:
                GLib.source_remove(stop_timeout)

        # Stop RPC has returned stoppedPaths and delivered() has completed on GTK,
        # while .opus publication is still pending in the background:
        assert len(publication_futures) == 1 and not publication_futures[0].done()
        assert managed.stopping and not managed.finished
        assert list(library_root.glob("*.opus")) == []

        # Fresh broker polls arrive while publication is still pending:
        app._browser_snapshot = advance_capture_snapshot(app, browser_service.request())
        app._browser_answered_at = time.monotonic()
        window._browser_snapshot = app._browser_snapshot
        watcher.poll()
        spin_until(lambda: watcher.future is None)

        assert app._browser_capture_phases.get(identifier) == "stopping"
        assert watcher.snapshot == {identifier: "saving"}
        assert application_activity(app, watcher) == {identifier: "saving"}
        scanned_during_pub = LibraryScanner(library_root).scan(capture_activity=application_activity(app, watcher))
        assert scanned_during_pub.meetings[0].lifecycle == "saving"
        assert capture_guard.capture_targets(app)[1] is True

        # Now release background publication and confirm broker completion clears both:
        publication_futures[0].set_result(object())
        assert managed.stopped.wait(2)
        assert managed.finished and not managed.stopping

        app._browser_snapshot = advance_capture_snapshot(app, browser_service.request())
        app._browser_answered_at = time.monotonic()
        window._browser_snapshot = app._browser_snapshot
        watcher.poll()
        spin_until(lambda: watcher.future is None and watcher.snapshot == {})

        assert app._browser_capture_phases.get(identifier) == "finished"
        assert application_activity(app, watcher) == {}
        assert capture_guard.capture_targets(app) == ([], False)
        scanned_after_pub = LibraryScanner(library_root).scan(capture_activity=application_activity(app, watcher))
        assert scanned_after_pub.meetings[0].lifecycle == "saved"
    finally:
        if watcher is not None:
            watcher.close()
        for pending in publication_futures:
            if not pending.done():
                pending.set_result(object())
        stop_broker(broker, broker_thread, sock_path)
