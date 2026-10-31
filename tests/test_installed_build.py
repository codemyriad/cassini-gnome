# SPDX-License-Identifier: GPL-3.0-or-later
"""D42 detects same-version patches and never executes a partial install."""

import importlib.util
import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_browser_connection import Clock
from test_transcription import until

from cassini_talk.build_refresh import BuildRefresh
from cassini_talk.installed_build import InstalledBuild, decode, read

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("build_identity", ROOT / "build-aux/build-identity.py")
identity = importlib.util.module_from_spec(spec)
spec.loader.exec_module(identity)


def payload(root, text="old", *, version="0.3.0"):
    source, build = root / "source", root / "build"
    module = source / "src/cassini_talk/example.py"
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text(text)
    launcher = build / "src/cassini-talk"
    launcher.parent.mkdir(parents=True, exist_ok=True)
    launcher.write_text("#!/usr/bin/python3\n")
    launcher.chmod(0o755)
    value = identity.generate(source, build, [module, launcher], version, "default", "io.codemyriad.CassiniTalk")
    installed = root / "install"
    (installed / "cassini_talk").mkdir(parents=True, exist_ok=True)
    (installed / "cassini_talk/example.py").write_text(text)
    (installed / "build-info.json").write_text(json.dumps(value))
    return value, installed, launcher


def test_same_version_patch_changes_content_identity_and_frozen_startup(tmp_path):
    first, root, launcher = payload(tmp_path)
    running = InstalledBuild(root, launcher, "0.3.0")
    assert not running.changed()
    second, _, _ = payload(tmp_path, "new")
    assert first["version"] == second["version"] and first["buildId"] != second["buildId"]
    assert running.changed() and running.running["buildId"] == first["buildId"]
    assert not InstalledBuild(root, launcher, "0.3.0").changed()


def test_manifest_published_before_files_never_restarts(tmp_path):
    _, root, launcher = payload(tmp_path)
    running = InstalledBuild(root, launcher, "0.3.0")
    payload(tmp_path, "new")
    module = root / "cassini_talk/example.py"
    module.write_text("old")
    with pytest.raises(OSError, match="still being installed"):
        running.changed()
    module.write_text("new")
    assert running.changed()


def test_new_launcher_must_be_executable_before_restart(tmp_path):
    _, root, launcher = payload(tmp_path)
    running = InstalledBuild(root, launcher, "0.3.0")
    payload(tmp_path, "new")
    launcher.chmod(0o644)
    with pytest.raises(OSError, match="still being installed"):
        running.changed()
    launcher.chmod(0o755)
    assert running.changed()


def test_identity_is_deterministic_and_hashes_every_input(tmp_path):
    first, _, _ = payload(tmp_path)
    second, _, _ = payload(tmp_path)
    assert first == second
    third, _, _ = payload(tmp_path, version="0.3.1")
    assert third["buildId"] != first["buildId"]


def test_engine_only_patch_changes_the_build_identity(tmp_path):
    _, _, launcher = payload(tmp_path)
    source, build = tmp_path / "source", tmp_path / "build"
    pins = source / "whistle/pins.json"
    pins.parent.mkdir()
    pins.write_text('{"engine": "old"}')
    first = identity.generate(source, build, [launcher, pins], "0.3.0", "default", "io.codemyriad.CassiniTalk")
    pins.write_text('{"engine": "new"}')
    second = identity.generate(source, build, [launcher, pins], "0.3.0", "default", "io.codemyriad.CassiniTalk")
    assert first["buildId"] != second["buildId"]
    assert first["files"]["whistle/pins.json"] != second["files"]["whistle/pins.json"]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.update(schema=True),
        lambda value: value["files"].update({"../outside": "0" * 64}),
        lambda value: value["files"].update({"/absolute": "0" * 64}),
        lambda value: value.update(buildId="0" * 64),
    ],
)
def test_damaged_or_unsafe_metadata_is_rejected(tmp_path, mutate):
    value, _, _ = payload(tmp_path)
    mutate(value)
    with pytest.raises(ValueError):
        decode(json.dumps(value).encode())


def test_fifo_and_symlink_never_block_identity_reads(tmp_path):
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    with pytest.raises(OSError):
        read(fifo)
    link = tmp_path / "link"
    link.symlink_to(fifo)
    with pytest.raises(OSError):
        read(link)


def application():
    held, finished, windows = [], [], []
    app = SimpleNamespace(
        hold=lambda: held.append(True),
        release=lambda: held.pop(),
        finish_quit=lambda **_kwargs: finished.append(True),
        get_windows=lambda: windows,
        browser_watch=None,
        native_captures={},
        _browser_snapshot={},
        _auto_recording_held=True,
        _transcription_holds=0,
    )
    return app, held, finished, windows


def controller(app, installed):
    clock = Clock()
    refresh = BuildRefresh(
        app,
        installed,
        post=clock.post,
        later=lambda ms, callback: clock.later(ms / 1000, callback),
        cancel=lambda key: clock.timers.pop(key),
    )
    return refresh, clock


def test_idle_upgrade_restarts_and_carries_requested_window_files(tmp_path):
    app, held, finished, _ = application()
    installed = SimpleNamespace(changed=lambda: True, restart=None)
    refresh, clock = controller(app, installed)
    opened = []
    refresh.check(lambda: opened.append(True), show=True, files=["file:///meeting.opus"])
    clock.finish()
    assert finished == [True] and not opened and not held
    assert installed.restart == dict(show=True, automatic=True, files=("file:///meeting.opus",), writing=False)


def test_active_capture_defers_then_finishing_still_blocks_restart():
    app, held, finished, windows = application()
    recorder = SimpleNamespace(pipeline=object(), _finishing=False)
    app.native_captures = {"one": recorder}
    banners = []
    windows.append(SimpleNamespace(get_visible=lambda: True, update_build_banner=lambda: banners.append(True)))
    installed = SimpleNamespace(changed=lambda: True, restart=None)
    refresh, clock = controller(app, installed)
    opened = []
    refresh.check(lambda: opened.append(True), show=True)
    clock.finish()
    assert refresh.pending and not finished and opened and not held
    recorder.pipeline = None
    recorder._finishing = True
    refresh.capture_changed()
    assert not finished and not refresh.future
    recorder._finishing = False
    refresh.capture_changed()
    clock.finish()
    assert finished == [True] and installed.restart["show"] is True


def test_hidden_restart_keeps_automatic_state_and_pending_writing():
    app, held, finished, _ = application()
    app._transcription_holds = 1
    installed = SimpleNamespace(changed=lambda: True, restart=None)
    refresh, clock = controller(app, installed)
    refresh.check()
    clock.finish()
    assert finished and not held
    assert installed.restart == dict(show=False, automatic=True, files=(), writing=True)


def test_same_build_runs_activation_without_restart():
    app, held, finished, _ = application()
    refresh, clock = controller(app, SimpleNamespace(changed=lambda: False, restart=None))
    called = []
    refresh.check(lambda: called.append(True), show=True)
    clock.finish()
    assert called and not held and not finished and not refresh.pending


def test_close_cancels_retry_and_releases_check_hold():
    app, held, _, _ = application()

    def partial():
        raise OSError("partial")

    refresh, clock = controller(app, SimpleNamespace(changed=partial, restart=None))
    refresh.check()
    clock.finish()
    assert held and clock.timers
    refresh.close()
    assert not held and not clock.timers


def test_live_broker_capture_overrides_an_idle_gtk_snapshot(monkeypatch):
    from cassini_talk import browser_service

    app, held, finished, _ = application()
    app.browser_watch = object()
    monkeypatch.setattr(browser_service, "request", lambda: dict(statuses=[dict(recording=True)]))
    refresh, clock = controller(app, SimpleNamespace(changed=lambda: True, restart=None))
    refresh.check()
    clock.finish()
    assert refresh.pending and not finished and not held


def test_many_activations_coalesce_one_io_check_and_close_balances_hold():
    import threading

    app, held, finished, _ = application()
    entered, release = threading.Event(), threading.Event()
    calls = []

    def changed():
        calls.append(True)
        entered.set()
        release.wait(3)
        return False

    refresh, clock = controller(app, SimpleNamespace(changed=changed, restart=None))
    refresh.check()
    until(entered.is_set)
    for _ in range(20):
        refresh.check(show=True)
    assert calls == [True] and len(held) == 1
    refresh.close()
    release.set()
    clock.finish()
    assert not held and not finished


def test_flatpak_checks_current_host_deployment_instead_of_running_app_mount(tmp_path, monkeypatch):
    from cassini_talk import flatpak_bridge as bridge

    _, old_root, old_launcher = payload(tmp_path / "old")
    running = InstalledBuild(old_root, old_launcher, "0.3.0", flatpak=True)
    value, new_root, new_launcher = payload(tmp_path / "new", "new")
    location = tmp_path / "deployment"
    root = location / "files/share/cassini-talk"
    shutil.copytree(new_root, root)
    launcher = location / "files/bin/cassini-talk"
    launcher.parent.mkdir(parents=True)
    shutil.copyfile(new_launcher, launcher)
    fake = tmp_path / "flatpak"
    fake.write_text(f"#!/bin/sh\nprintf '%s\\n' '{location}'\n")
    fake.chmod(0o700)
    monkeypatch.setattr(
        bridge, "deployment", lambda: bridge.Deployment("io.codemyriad.CassiniTalk", "x86_64", "stable")
    )
    monkeypatch.setattr(bridge, "host_info", lambda: (None, None, str(fake), None))
    monkeypatch.setattr(bridge, "host_command", lambda command, **_: command)
    assert running.changed()
    assert read(old_root / "build-info.json") == running.running
    assert value["buildId"] != running.running["buildId"]
    (root / "cassini_talk/example.py").write_text("incomplete deployment")
    with pytest.raises(subprocess.CalledProcessError):
        running.changed()


def test_native_reexec_uses_installed_shebang_and_preserves_hidden_handoff(tmp_path, monkeypatch):
    _, root, launcher = payload(tmp_path)
    running = InstalledBuild(root, launcher, "0.3.0")
    calls = []
    monkeypatch.setattr(os, "execv", lambda program, args: calls.append((program, args)))
    running.reexec(show=False, automatic=True, writing=True, files=["file:///meeting.opus"])
    assert calls == [
        (
            str(launcher),
            [
                str(launcher),
                "file:///meeting.opus",
                "--cassini-resume-background",
                "--cassini-resume-writing",
                "--cassini-resume-automatic",
            ],
        )
    ]


def test_activation_callback_runs_before_last_hold_is_released():
    app, held, _, _ = application()
    refresh, clock = controller(app, SimpleNamespace(changed=lambda: False, restart=None))
    observed = []
    refresh.check(lambda: observed.append(bool(held)), show=True)
    clock.finish()
    assert observed == [True] and not held


def test_record_request_hands_off_to_new_build_instead_of_starting_old_code():
    app, held, finished, _ = application()
    installed = SimpleNamespace(changed=lambda: True, restart=None)
    refresh, clock = controller(app, installed)
    recorded = []
    refresh.check(lambda: recorded.append(True), show=True, record=True)
    clock.finish()
    assert finished and not recorded and not held
    assert installed.restart["record"] is True


def test_record_handoff_has_a_private_launcher_argument(tmp_path, monkeypatch):
    _, root, launcher = payload(tmp_path)
    running = InstalledBuild(root, launcher, "0.3.0")
    calls = []
    monkeypatch.setattr(os, "execv", lambda program, args: calls.append(args))
    running.reexec(show=True, automatic=False, record=True)
    assert calls == [[str(launcher), "--cassini-resume-record"]]


@pytest.mark.parametrize("guard", ["_capture_background", "_capture_background_saving", "_capture_confirmation"])
def test_hidden_save_and_confirmation_guard_restart_until_finished(guard):
    app, held, finished, _ = application()
    setattr(app, guard, True)
    refresh, clock = controller(app, SimpleNamespace(changed=lambda: True, restart=None))
    refresh.check()
    clock.finish()
    assert refresh.pending and not refresh.can_restart and not finished and not held
    setattr(app, guard, False)
    refresh.capture_changed()
    clock.finish()
    assert finished and not held


def test_preparation_is_durable_before_restart_and_new_queue_can_resume(tmp_path, monkeypatch):
    import threading

    from test_library import capture

    from cassini_talk.library import LibraryScanner
    from cassini_talk.meeting_store import read_metadata
    from cassini_talk.transcription import TranscriptionQueue

    monkeypatch.setenv("CASSINI_LIBRARY_DIR", str(tmp_path))
    session = capture(tmp_path)
    entered, release, working, finish_job = (threading.Event() for _ in range(4))
    app, held, finished, _ = application()
    app._transcription_holds = 1
    refresh, clock = controller(app, SimpleNamespace(changed=lambda: True, restart=None))
    changes = []
    queue = app.transcriber = TranscriptionQueue(changes.append, lambda _: None, root=tmp_path, bundle=tmp_path)
    original = queue._mark_queued

    def blocked(*args):
        entered.set()
        assert release.wait(5)
        return original(*args)

    def job(*_args):
        working.set()
        assert finish_job.wait(5)

    monkeypatch.setattr(queue, "_mark_queued", blocked)
    monkeypatch.setattr(queue, "_job", job)

    def quit(**_kwargs):
        assert read_metadata(session.directory)["transcript"]["status"] == "queued"
        queue.close()
        finished.append(True)

    app.finish_quit = quit
    try:
        assert queue.enqueue(session.directory)
        until(entered.is_set)
        refresh.check()
        clock.finish()
        assert not finished and queue.preparing() and not queue.closed
        assert read_metadata(session.directory)["transcript"]["status"] == "none"
        release.set()
        until(working.is_set)
        until(lambda: bool(changes))
        assert not queue.preparing() and queue.pending, "Inference must remain resumable without waiting for it"
        refresh.capture_changed()
        clock.finish()
        assert finished and not held and refresh.installed.restart["writing"]
        meeting = LibraryScanner(tmp_path).scan().meetings[0]
        assert meeting.transcript == "queued"
        resumed = TranscriptionQueue(lambda _: None, lambda _: None, root=tmp_path, bundle=tmp_path)
        observed = []
        monkeypatch.setattr(resumed, "_job", lambda *_args: observed.append(True))
        try:
            assert resumed.enqueue(meeting.directory, resume=True)
            until(lambda: bool(observed))
        finally:
            resumed.close()
    finally:
        release.set()
        finish_job.set()
        queue.close()
        queue.executor.shutdown(wait=True)


class DeferredTrash:
    def __init__(self):
        self.pending = True

    def flush(self, done, *, failed):
        self.done, self.failed = done, failed
        return self.pending


def test_trash_failure_restores_activation_and_future_restart():
    app, held, finished, _ = application()
    app.meeting_trash = trash = DeferredTrash()
    installed = SimpleNamespace(changed=lambda: True, restart=None)
    refresh, clock = controller(app, installed)
    called = []
    refresh.check(lambda: called.append("activate"), show=True)
    clock.finish()
    assert refresh.flushing and not refresh.restarting and held and not refresh.can_restart
    refresh.check(lambda: called.append("open"), files=["file:///meeting.opus"])
    refresh.check(lambda: called.append("record"), record=True)
    trash.pending = False
    trash.failed()
    assert called == ["activate", "open", "record"] and not finished and not held
    assert not refresh.flushing and not refresh.restarting and installed.restart is None
    assert not refresh.show and not refresh.record and not refresh.files and not refresh.callbacks
    refresh.check()
    clock.finish()
    assert finished and not installed.restart.get("record") and installed.restart["files"] == ()


def test_capture_started_during_trash_flush_is_rechecked_before_quitting():
    app, held, finished, _ = application()
    app.meeting_trash = trash = DeferredTrash()
    refresh, clock = controller(app, SimpleNamespace(changed=lambda: True, restart=None))
    refresh.check()
    clock.finish()
    recorder = SimpleNamespace(pipeline=object(), _finishing=False)
    app.native_captures = {"new": recorder}
    trash.pending = False
    trash.done()
    clock.finish()
    assert not finished and not held and not refresh.restarting and not refresh.can_restart
    recorder.pipeline = None
    refresh.capture_changed()
    clock.finish()
    assert finished and not held


def test_changed_broker_state_during_worker_requires_a_fresh_check(monkeypatch):
    import threading

    from cassini_talk import browser_service

    app, held, finished, _ = application()
    app.browser_watch = object()
    entered, release = threading.Event(), threading.Event()
    replies = []

    def request():
        if not replies:
            replies.append(True)
            entered.set()
            assert release.wait(5)
            return dict(statuses=[dict(recording=True)])
        replies.append(False)
        return dict(statuses=[])

    monkeypatch.setattr(browser_service, "request", request)
    refresh, clock = controller(app, SimpleNamespace(changed=lambda: True, restart=None))
    refresh.check()
    until(entered.is_set)
    refresh.check()  # Stop completed after the worker observed the old state.
    release.set()
    clock.finish()
    assert not finished and held
    clock.finish()
    assert replies == [True, False] and finished and not held


def test_live_broker_state_disables_restart_banner(monkeypatch):
    from cassini_talk import browser_service

    app, held, finished, windows = application()
    app.browser_watch = object()
    monkeypatch.setattr(browser_service, "request", lambda: dict(statuses=[dict(recording=True)]))
    refresh, clock = controller(app, SimpleNamespace(changed=lambda: True, restart=None))
    enabled = []
    windows.append(SimpleNamespace(update_build_banner=lambda: enabled.append(refresh.can_restart)))
    refresh.check()
    clock.finish()
    assert refresh.browser_busy and refresh.pending and enabled == [False]
    assert not finished and not held


def test_partial_install_retry_exhaustion_clears_intent_and_next_request_retries():
    app, held, finished, _ = application()
    calls = []

    def changed():
        calls.append(True)
        raise OSError("generated incomplete install")

    refresh, clock = controller(app, SimpleNamespace(changed=changed, restart=None))
    called = []
    refresh.check(lambda: called.append(True), show=True, files=["file:///old.opus"], record=True)
    for attempt in range(21):
        clock.finish()
        if attempt < 20:
            clock.advance(0.5)
    assert len(calls) == 21 and called == [True] and not held and not finished
    assert refresh.attempts == 0 and not refresh.show and not refresh.record and not refresh.files
    refresh.check()
    clock.finish()
    assert len(calls) == 22 and refresh.attempts == 1 and held and clock.timers
    refresh.close()
    assert not held


def test_visible_reexec_preserves_automatic_recording_hold(tmp_path, monkeypatch):
    _, root, launcher = payload(tmp_path)
    running = InstalledBuild(root, launcher, "0.3.0")
    calls = []
    monkeypatch.setattr(os, "execv", lambda program, args: calls.append(args))
    running.reexec(show=True, automatic=True, files=["file:///meeting.opus"], record=True)
    assert calls == [[str(launcher), "file:///meeting.opus", "--cassini-resume-record", "--cassini-resume-automatic"]]
