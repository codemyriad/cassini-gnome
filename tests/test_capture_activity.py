# SPDX-License-Identifier: GPL-3.0-or-later
"""Private broker/controller state; never infer recording from file timestamps."""

from types import SimpleNamespace

import pytest

pytest.importorskip("gi")
from test_browser_service import browser, service, wait_for  # noqa: E402, F401
from test_library import capture  # noqa: E402
from test_library_monitor import spin_until  # noqa: E402

from cassini_viewer.browser_service import send_packet  # noqa: E402
from cassini_viewer.capture_activity import BrowserActivity, read_browser_activity, recorder_activity  # noqa: E402


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

    from cassini_viewer import browser_watch, companion

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
        lambda _: None, path=service[1], profiles_changed=lambda: notifications.append(threading.get_ident())
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


def test_native_identity_only_comes_from_controller_owned_session(tmp_path):
    session = capture(tmp_path)
    recorder = SimpleNamespace(pipeline=object(), session=session, stopping=False)
    window = SimpleNamespace(recorder=recorder)
    assert recorder_activity([window]) == {session.manifest["id"]: "recording"}
    recorder.stopping = True
    assert recorder_activity([window]) == {session.manifest["id"]: "saving"}
    recorder.pipeline = None
    assert recorder_activity([window]) == {}
    standalone = SimpleNamespace(
        recorder=SimpleNamespace(pipeline=object(), path=str(tmp_path / "cassini-2026-10-27.opus"), stopping=False)
    )
    assert not recorder_activity([standalone]), "Standalone audio must never acquire an invented Library UUID"


@pytest.mark.parametrize("failure", ["timeout", "exit"])
def test_desktop_probe_failure_preserves_watch_lease_and_status_polling(service, tmp_path, monkeypatch, failure):  # noqa: F811
    import subprocess

    from cassini_viewer import browser_watch, companion, flatpak_bridge

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
        flatpak_bridge, "deployment", lambda: flatpak_bridge.Deployment("com.gocassini.Viewer", "x86_64", "stable")
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
        statuses.append, path=service[1], profiles_changed=lambda: profiles.append(watcher._profiles)
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
        assert len(statuses) == delivered + 1, "Broker status polling must continue after the probe fails"
        assert profiles[-1] == "unavailable"
    finally:
        watcher.close()
