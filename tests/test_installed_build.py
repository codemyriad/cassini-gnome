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

from cassini_viewer.build_refresh import BuildRefresh
from cassini_viewer.installed_build import InstalledBuild, decode, read

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("build_identity", ROOT / "build-aux/build-identity.py")
identity = importlib.util.module_from_spec(spec)
spec.loader.exec_module(identity)


def payload(root, text="old", *, version="0.3.0"):
    source, build = root / "source", root / "build"
    module = source / "src/cassini_viewer/example.py"
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text(text)
    launcher = build / "src/cassini-viewer"
    launcher.parent.mkdir(parents=True, exist_ok=True)
    launcher.write_text("#!/usr/bin/python3\n")
    launcher.chmod(0o755)
    value = identity.generate(source, build, [module, launcher], version, "default", "com.gocassini.Viewer")
    installed = root / "install"
    (installed / "cassini_viewer").mkdir(parents=True, exist_ok=True)
    (installed / "cassini_viewer/example.py").write_text(text)
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
    module = root / "cassini_viewer/example.py"
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
        finish_quit=lambda: finished.append(True),
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
    from cassini_viewer import browser_service

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
    from cassini_viewer import flatpak_bridge as bridge

    _, old_root, old_launcher = payload(tmp_path / "old")
    running = InstalledBuild(old_root, old_launcher, "0.3.0", flatpak=True)
    value, new_root, new_launcher = payload(tmp_path / "new", "new")
    location = tmp_path / "deployment"
    root = location / "files/share/cassini-viewer"
    shutil.copytree(new_root, root)
    launcher = location / "files/bin/cassini-viewer"
    launcher.parent.mkdir(parents=True)
    shutil.copyfile(new_launcher, launcher)
    fake = tmp_path / "flatpak"
    fake.write_text(f"#!/bin/sh\nprintf '%s\\n' '{location}'\n")
    fake.chmod(0o700)
    monkeypatch.setattr(bridge, "deployment", lambda: bridge.Deployment("com.gocassini.Viewer", "x86_64", "stable"))
    monkeypatch.setattr(bridge, "host_info", lambda: (None, None, str(fake), None))
    monkeypatch.setattr(bridge, "host_command", lambda command, **_: command)
    assert running.changed()
    assert read(old_root / "build-info.json") == running.running
    assert value["buildId"] != running.running["buildId"]
    (root / "cassini_viewer/example.py").write_text("incomplete deployment")
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
