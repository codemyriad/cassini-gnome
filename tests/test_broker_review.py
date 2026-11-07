# SPDX-License-Identifier: GPL-3.0-or-later
"""Real private broker clients with controlled host probes and retirement ordering."""

import contextlib
import json
import os
import signal
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from test_broker_identity import BOUND, close_broker, private  # noqa: F401
from test_installed_build import application, controller, payload

from cassini_talk import broker_identity, broker_lifecycle, browser_service, flatpak_bridge, installed_build
from cassini_talk.browser_audio_proxy import BrowserAudioProxy


@pytest.fixture
def host(private, tmp_path, monkeypatch):  # noqa: F811
    value, root, launcher = payload(tmp_path / "local", version="0.4.1")
    local = dict(app="cassini-talk", version=value["version"], buildId=value["buildId"], moduleRoot=str(root))
    state = dict(value=value, local=local, root=root, launcher=launcher, calls=[], error=None)
    monkeypatch.setenv("FLATPAK_ID", "io.codemyriad.CassiniTalk.Devel")
    monkeypatch.setattr(broker_identity, "_host_targets", {}, raising=False)
    monkeypatch.setattr(broker_identity, "expected", lambda: dict(local))
    monkeypatch.setattr(
        flatpak_bridge, "deployment", lambda: flatpak_bridge.Deployment(os.environ["FLATPAK_ID"], "x86_64", "master")
    )
    monkeypatch.setattr(flatpak_bridge, "host_command", lambda command, **_: ["flatpak-spawn", "--host", *command])

    def run(command, **kwargs):
        assert command[:2] == ["flatpak-spawn", "--host"]
        state["calls"].append((threading.get_ident(), command))
        stage = command[2]
        if state.get("entered") is not None and len(state["calls"]) == 1:
            state["entered"].set()
            assert state["release"].wait(BOUND), "release the worker's host probe"
        if state["error"] is not None and stage == "/usr/bin/flatpak":
            raise state["error"]
        if stage == "/bin/sh":
            output = "\0".join(
                ["/host/config", "/host/data", "/usr/bin/flatpak", *("" for _ in flatpak_bridge.COMMANDS), ""]
            ).encode()
        elif stage == "/usr/bin/flatpak":
            output = b"/host/deployment\n"
        elif stage == "head":
            output = json.dumps(state["value"]).encode()
        elif stage == "sha256sum":
            assert state["value"]["files"]["cassini_talk/example.py"].encode() in kwargs["input"]
            output = b""
        else:
            pytest.fail("Unexpected host command: " + repr(command))
        return SimpleNamespace(stdout=output, stderr=b"", returncode=0)

    monkeypatch.setattr(subprocess, "run", run)
    return state


@contextlib.contextmanager
def serving(path):
    service = browser_service.BrowserService(path.with_suffix(".json"), notifier=lambda *_: None)
    ready = threading.Event()
    thread = threading.Thread(target=service.serve, args=(path, ready), daemon=True)
    thread.start()
    assert ready.wait(BOUND)
    try:
        yield service
    finally:
        getattr(service, "close", service.closed.set)()
        with contextlib.suppress(OSError), browser_service.connect(path):
            pass
        thread.join(BOUND)
        assert not thread.is_alive()


@pytest.mark.parametrize(
    "error", [None, subprocess.CalledProcessError(1, "flatpak"), subprocess.TimeoutExpired("flatpak", 5)]
)
def test_flatpak_polls_status_and_audio_share_one_host_probe_per_build(host, private, monkeypatch, error):  # noqa: F811
    host["error"] = error
    pytest.importorskip("gi")
    from cassini_talk.browser_watch import BrowserWatch

    monkeypatch.setattr(browser_service, "service_path", lambda: private)
    with serving(private), ThreadPoolExecutor(max_workers=1) as worker:
        watch = BrowserWatch.__new__(BrowserWatch)
        watch.profiles_changed = None
        watch.path = None  # Exercise ensure_service AND the reply's diagnostic.
        watch.closed = threading.Event()
        watch.lock = threading.Lock()
        watch.lease = None
        watch.active, watch.generation, watch.timer = True, 1, 0
        watch.executor = worker
        try:
            lease = None
            for _ in range(12):
                reply = worker.submit(watch._read).result(BOUND)
                assert reply["brokerStale"] is False
                lease = lease or watch.lease
                assert watch.lease is lease and lease.fileno() >= 0
            assert not browser_service.request(path=private)["brokerStale"]
            with contextlib.closing(BrowserAudioProxy(path=private)) as proxy:
                assert not proxy.broker_stale
            assert len(host["calls"]) == (3 if error is None else 2), (
                "12 polls and audio must not each launch host probes"
            )
        finally:
            watch.close()


@pytest.mark.parametrize(
    "error", [subprocess.CalledProcessError(1, "flatpak"), subprocess.TimeoutExpired("flatpak", 5)]
)
def test_failed_flatpak_probe_is_cached_and_cannot_drop_valid_status_or_audio(host, private, error):  # noqa: F811
    host["error"] = error
    with serving(private):
        for _ in range(12):
            assert browser_service.ensure_service(private) == private
            assert browser_service.request(path=private)["identity"]["buildId"] == host["local"]["buildId"]
            with contextlib.closing(BrowserAudioProxy(path=private)) as proxy:
                assert not proxy.broker_stale
        assert len(host["calls"]) == 2, "failed discovery must not repeat on each diagnostic lookup"
    with pytest.raises(OSError, match="inspect installed Flatpak identity"):
        installed_build.host_identity()


def test_verified_new_host_build_replaces_the_cached_target(host, tmp_path):
    assert broker_identity.target()["buildId"] == host["value"]["buildId"]
    original_calls = len(host["calls"])
    current = installed_build.InstalledBuild(host["root"], host["launcher"], "0.4.1", flatpak=True)
    newer, _, _ = payload(tmp_path / "next", text="new", version="0.4.2")
    host["value"] = newer
    assert broker_identity.target()["buildId"] != newer["buildId"]
    assert len(host["calls"]) == original_calls
    assert current.changed()
    after_refresh = len(host["calls"])
    assert after_refresh == original_calls + 4  # Fresh discovery AND verified payload.
    for _ in range(12):
        assert broker_identity.target()["buildId"] == newer["buildId"]
    assert len(host["calls"]) == after_refresh


def test_initial_host_probe_never_waits_or_executes_on_the_gtk_context(host):
    pytest.importorskip("gi")
    from gi.repository import GLib

    host["entered"], host["release"] = threading.Event(), threading.Event()
    gtk_thread = threading.get_ident()
    context = GLib.MainContext.default()
    assert context.acquire()
    try:
        assert broker_identity.target() == host["local"]
        assert host["entered"].wait(BOUND)
        assert not host["release"].is_set(), "GTK must return before host IO is released"
    finally:
        context.release()
        host["release"].set()
    with ThreadPoolExecutor(max_workers=1) as worker:
        assert worker.submit(broker_identity.target).result(BOUND) == host["local"]
    assert len(host["calls"]) == 3
    assert all(thread != gtk_thread for thread, _ in host["calls"])


def test_native_manifest_cannot_publish_a_mixed_payload_identity(tmp_path):
    _, root, _ = payload(tmp_path, text="old")
    newer, _, _ = payload(tmp_path, text="new")
    module = root / "cassini_talk/example.py"
    module.write_text("old")
    with pytest.raises(OSError, match="still being installed"):
        broker_identity.expected(root)
    module.write_text("new")
    assert broker_identity.expected(root)["buildId"] == newer["buildId"]


@pytest.mark.parametrize("flag", ["stopping", "starting"])
def test_legacy_capture_save_and_start_are_busy_without_a_phase(flag, monkeypatch, tmp_path):
    status = dict(statuses=[dict(recording=False, **{flag: True})])
    monkeypatch.setattr(broker_lifecycle, "inspect", lambda _: (status, (123, os.getuid(), 0)))
    monkeypatch.setattr(broker_identity, "target", lambda: pytest.fail("A busy capture never needs host discovery"))
    monkeypatch.setattr(os, "pidfd_open", lambda *_: pytest.fail("A busy legacy capture must never be signalled"))
    assert broker_lifecycle.reusable(object(), tmp_path / "b.sock") is True


def test_build_refresh_can_recheck_after_retirement_unlinked_the_broker(monkeypatch):
    app, held, finished, _ = application()
    app.browser_watch = object()
    calls, flushes = [], []

    def status():
        calls.append(True)
        if len(calls) > 1:
            raise FileNotFoundError("retired broker")
        return dict(statuses=[], meetings=[])

    def flush(callback, **_):
        flushes.append(callback)
        return len(flushes) == 1

    app.meeting_trash = SimpleNamespace(flush=flush)
    monkeypatch.setattr(browser_service, "request", status)
    monkeypatch.setattr(broker_lifecycle, "retire_idle", lambda: False)
    refresh, clock = controller(app, SimpleNamespace(changed=lambda: True, restart=None))
    refresh.check()
    clock.finish()
    assert refresh.flushing and not finished
    flushes[0]()
    clock.finish()
    assert finished == [True] and not held and refresh.attempts == 0
    assert not clock.timers and len(calls) == 2


def test_retire_idle_does_not_reconnect_after_it_retired_the_listener(tmp_path, monkeypatch):
    calls = []

    @contextlib.contextmanager
    def connect(*_, **__):
        calls.append(True)
        assert len(calls) == 1, "the retired socket must not be reconnected"
        yield object()

    monkeypatch.setattr(browser_service, "connect", connect)
    monkeypatch.setattr(broker_lifecycle, "reusable", lambda *_: False)
    assert broker_lifecycle.retire_idle(tmp_path / "b.sock") is False


@pytest.mark.parametrize("stage", ["inspect", "shutdown", "legacy-recheck"])
def test_peer_exiting_during_retirement_still_waits_for_its_lock(stage, tmp_path, monkeypatch):
    waited = []
    status = dict(identity={}, idleShutdown=stage != "legacy-recheck", statuses=[], meetings=[])

    def inspect(_):
        if stage == "inspect":
            raise BrokenPipeError("peer closing")
        return status, (123, os.getuid(), 0)

    def gone(*_, **__):
        raise FileNotFoundError("peer already retired")

    monkeypatch.setattr(broker_lifecycle, "inspect", inspect)
    monkeypatch.setattr(broker_lifecycle, "_released", lambda *_: waited.append(True))
    monkeypatch.setattr(browser_service, "request", gone)
    monkeypatch.setattr(browser_service, "connect", gone)
    monkeypatch.setattr(broker_lifecycle, "_legacy_process", lambda *_: True)
    monkeypatch.setattr(os, "pidfd_open", lambda *_: os.open("/dev/null", os.O_RDONLY))
    monkeypatch.setattr(signal, "pidfd_send_signal", lambda *_: pytest.fail("No signal after the peer is gone"))
    assert broker_lifecycle.reusable(object(), tmp_path / "b.sock") is False
    assert waited == [True]


def test_simultaneous_identity_lookups_share_the_same_initial_host_probe(host):
    host["entered"], host["release"] = threading.Event(), threading.Event()
    together = threading.Barrier(5)

    def target():
        together.wait(BOUND)
        return broker_identity.target()

    with ThreadPoolExecutor(max_workers=4) as workers:
        futures = [workers.submit(target) for _ in range(4)]
        try:
            together.wait(BOUND)
            assert host["entered"].wait(BOUND)
        finally:
            host["release"].set()
        assert all(future.result(BOUND) == host["local"] for future in futures)
    assert len(host["calls"]) == 3


def test_concurrent_ensure_waits_for_retiring_lock_before_starting_one_replacement(private, monkeypatch):  # noqa: F811
    entered, release, contended = threading.Event(), threading.Event(), threading.Event()
    service = browser_service.BrowserService(private.with_suffix(".json"), notifier=lambda *_: None)
    service.identity["buildId"] = "stale"
    shutdown = service.workers.shutdown

    def hold_release(**kwargs):
        entered.set()
        assert release.wait(BOUND), "release the retiring broker's ownership lock"
        shutdown(**kwargs)

    monkeypatch.setattr(service.workers, "shutdown", hold_release)
    ready = threading.Event()
    thread = threading.Thread(target=service.serve, args=(private, ready), daemon=True)
    thread.start()
    assert ready.wait(BOUND)
    flock = broker_lifecycle.fcntl.flock

    def observe_lock(descriptor, flags):
        try:
            return flock(descriptor, flags)
        except BlockingIOError:
            if threading.current_thread().name == "second-ensure":
                contended.set()
            raise

    monkeypatch.setattr(broker_lifecycle.fcntl, "flock", observe_lock)
    popen, launches = subprocess.Popen, []

    def spawn(*args, **kwargs):
        assert release.is_set(), "never spawn while the old broker still owns its lock"
        launches.append(True)
        return popen(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", spawn)
    answers, errors = [], []

    def ensure():
        try:
            answers.append(browser_service.ensure_service(private))
        except Exception as error:
            errors.append(error)

    first = threading.Thread(target=ensure, name="first-ensure")
    second = threading.Thread(target=ensure, name="second-ensure")
    try:
        first.start()
        assert entered.wait(BOUND), "old socket unlinked, old lock still held"
        assert not private.exists()
        second.start()
        assert contended.wait(BOUND), "concurrent client waits inside the same handover"
        assert not answers and not launches
    finally:
        release.set()
        first.join(BOUND)
        if second.ident is not None:
            second.join(BOUND)
        thread.join(BOUND)
    try:
        assert not first.is_alive() and not second.is_alive() and not thread.is_alive()
        assert not errors and answers == [private, private] and launches == [True]
        with (
            contextlib.closing(BrowserAudioProxy(path=private)) as one,
            contextlib.closing(BrowserAudioProxy(path=private)) as two,
        ):
            assert one.identity == two.identity
            assert one.identity["pid"] != os.getpid()
    finally:
        close_broker(private)


def test_verified_refresh_wins_over_an_older_inflight_host_probe(host, tmp_path):
    host["entered"], host["release"] = threading.Event(), threading.Event()
    newer, _, _ = payload(tmp_path / "next", text="new", version="0.4.2")
    with ThreadPoolExecutor(max_workers=1) as worker:
        pending = worker.submit(broker_identity.target)
        try:
            assert host["entered"].wait(BOUND)
            broker_identity.remember_host_identity(newer)
        finally:
            host["release"].set()
        assert pending.result(BOUND)["buildId"] == newer["buildId"]
    assert broker_identity.target()["buildId"] == newer["buildId"]
    assert len(host["calls"]) == 3


@pytest.mark.parametrize("client", ["status", "audio"])
def test_cold_diagnostics_return_valid_reply_before_host_probe_finishes(host, private, client):  # noqa: F811
    host["entered"], host["release"] = threading.Event(), threading.Event()
    with serving(private):
        try:
            if client == "status":
                assert browser_service.request(path=private)["identity"]["buildId"] == host["local"]["buildId"]
            else:
                with contextlib.closing(BrowserAudioProxy(path=private, spawn=False)) as proxy:
                    assert proxy.identity["buildId"] == host["local"]["buildId"]
            assert host["entered"].wait(BOUND)
            assert not host["release"].is_set(), "diagnostics must not gate a valid status or audio handshake"
        finally:
            host["release"].set()
        assert broker_identity.target() == host["local"]
    assert len(host["calls"]) == 3
