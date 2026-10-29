# SPDX-License-Identifier: GPL-3.0-or-later
"""D31 fake-clock retries and actual private user registration without a click."""

import errno
import threading
import time

import pytest
from test_transcription import until

from cassini_viewer import companion
from cassini_viewer.browser_connection import Backoff, BrowserConnection


class Clock:
    def __init__(self):
        self.now = 0
        self.next_id = 0
        self.timers = {}
        self.posted = []

    def later(self, delay, call):
        self.next_id += 1
        self.timers[self.next_id] = self.now + delay, call
        return self.next_id

    def post(self, call, *args):
        self.posted.append((call, args))

    def finish(self):
        until(lambda: bool(self.posted))
        call, args = self.posted.pop(0)
        call(*args)

    def advance(self, seconds):
        self.now += seconds
        due = [key for key, (at, _) in self.timers.items() if at <= self.now]
        for key in due:
            self.timers.pop(key)[1]()

    def connection(self, attempt, changed=lambda: None):
        return BrowserConnection(
            changed,
            attempt=attempt,
            later=self.later,
            post=self.post,
            cancel=lambda key: self.timers.pop(key),
            jitter=lambda *_: 0,
        )


def test_fake_clock_backoff_caps_at_five_minutes_and_trigger_resets_it():
    clock = Clock()
    attempts = []

    def failed():
        attempts.append(clock.now)
        raise OSError("temporary")

    controller = clock.connection(failed)
    try:
        controller.trigger()
        for delay in (5, 10, 20, 40, 80, 160, 300, 300):
            clock.finish()
            ((deadline, _),) = clock.timers.values()
            assert deadline - clock.now == delay
            count = len(attempts)
            clock.advance(delay - 0.01)
            assert len(attempts) == count
            clock.now = deadline
            clock.advance(0)
        clock.finish()
        controller.trigger()
        clock.finish()
        ((deadline, _),) = clock.timers.values()
        assert deadline - clock.now == 5
        assert controller.snapshot()["phase"] == "failed"
    finally:
        controller.close()


@pytest.mark.parametrize("jitter", [-0.2, 0.2])
def test_jitter_stays_in_range_and_never_exceeds_the_cap(jitter):
    retry = Backoff(lambda *_: jitter)
    base = 5
    for _ in range(12):
        value = retry.next()
        assert base * 0.8 <= value <= min(300, base * 1.2)
        base = min(300, base * 2)


@pytest.mark.parametrize(
    "error",
    [
        PermissionError(errno.EACCES, "config"),
        OSError(errno.EROFS, "config"),
        companion.SetupError("noBrowser"),
        companion.SetupError("unsafe", available=False),
    ],
)
def test_permanent_failures_stop_until_a_trigger(error):
    clock = Clock()
    controller = clock.connection(lambda: (_ for _ in ()).throw(error))
    try:
        controller.trigger()
        clock.finish()
        assert controller.snapshot()["phase"] == "failed" and not clock.timers
        clock.advance(10000)
        assert not clock.posted
        controller.trigger()
        clock.finish()
        assert controller.snapshot()["phase"] == "failed" and not clock.timers
    finally:
        controller.close()


def test_trigger_during_io_is_single_flight_and_close_never_waits_for_host():
    clock = Clock()
    entered, release = threading.Event(), threading.Event()
    threads, changes = [], []
    main = threading.get_ident()

    def blocked():
        threads.append(threading.get_ident())
        assert threads[-1] != main
        entered.set()
        assert release.wait(4)
        return ()

    controller = clock.connection(blocked, lambda: changes.append(threading.get_ident()))
    try:
        controller.trigger()
        assert entered.wait(2)
        controller.trigger()
        controller.trigger()
        assert len(threads) == 1 and controller.snapshot()["phase"] == "settingUp"
        release.set()
        clock.finish()
        clock.finish()
        assert len(threads) == 2 and controller.snapshot()["phase"] == "waiting"
        assert all(t == main for t in changes)
        entered.clear()
        release.clear()
        controller.trigger()
        assert entered.wait(2)
        before = time.monotonic()
        controller.close()
        assert time.monotonic() - before < 0.05
        count = len(changes)
        release.set()
        clock.finish()
        assert len(changes) == count
    finally:
        release.set()
        controller.close()


def test_startup_registers_both_hosts_preserves_mtime_and_repairs_an_updated_path(tmp_path, monkeypatch):
    browser = companion.Browser("Chromium", "/test/chromium", tmp_path / "config/chromium")
    executable = tmp_path / "host"
    executable.write_text("#!/bin/sh\n")
    executable.chmod(0o700)
    hosts = {"io.codemyriad.cassini": executable, "io.codemyriad.cassini.control": executable}
    monkeypatch.setattr(companion, "probe", lambda: (True, False))
    monkeypatch.setattr(companion, "installed_browsers", lambda: [browser])
    monkeypatch.setattr(companion, "host_paths", lambda: hosts)
    monkeypatch.setattr(companion, "in_flatpak", lambda: False)
    clock = Clock()
    from cassini_viewer.browser_connection import setup

    controller = clock.connection(setup)
    try:
        controller.trigger()  # Application launch, no Connect action.
        clock.finish()
        assert controller.snapshot()["phase"] == "waiting"
        files = tuple((browser.config / "NativeMessagingHosts").iterdir())
        assert {p.stem for p in files} == set(hosts)
        original = {p: (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_ino) for p in files}
        controller.trigger()
        clock.finish()
        assert original == {p: (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_ino) for p in files}
        updated = tmp_path / "new-host"
        updated.write_bytes(executable.read_bytes())
        updated.chmod(0o700)
        hosts.update({name: updated for name in hosts})
        controller.trigger()
        clock.finish()
        assert all(str(updated).encode() in p.read_bytes() for p in files)
        controller.observe(dict(connectedBrowsers=1, browserNames=["Chromium"]))
        assert controller.snapshot()["phase"] == "connected"
        controller.observe(dict(connectedBrowsers=0, browserNames=[]))
        assert controller.snapshot()["phase"] == "waiting"
    finally:
        controller.close()
