# SPDX-License-Identifier: GPL-3.0-or-later
"""Execute the actual host shell scripts against private configs and framed pipes."""

import json
import shutil
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from cassini_viewer import companion
from cassini_viewer import flatpak_bridge as bridge
from cassini_viewer.browser_control_host import parent_browser_info
from cassini_viewer.browser_service import service_path


@pytest.fixture
def host(tmp_path, monkeypatch):
    # Real shell metacharacters in host paths must stay literal arguments.
    directory = tmp_path / "host's space\n$(touch SHOULD-NOT-EXIST)"
    directory.mkdir()
    tools = directory / "bin"
    tools.mkdir()
    fake = tools / "flatpak"
    fake.write_text(
        f"#!{sys.executable}\nimport json,sys\nfrom pathlib import Path\n"
        f"Path({str(directory / 'arguments.json')!r}).write_text(json.dumps(sys.argv[1:]))\n"
        "sys.stdout.buffer.write(sys.stdin.buffer.read())\n"
    )
    fake.chmod(0o700)
    for command in ("google-chrome-stable", "chromium", "microsoft-edge", "brave"):
        path = tools / command
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(0o700)
    monkeypatch.setenv("PATH", str(tools) + ":/usr/bin:/bin")
    monkeypatch.setenv("HOME", str(directory))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(directory / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(directory / "data"))
    monkeypatch.setattr(bridge, "host_command", lambda command, **_: command)
    monkeypatch.setattr(
        bridge, "deployment", lambda: bridge.Deployment("com.gocassini.Viewer.Devel", "x86_64", "master")
    )
    return directory


def test_host_inventory_and_registration_use_host_configs_and_shell_safe_paths(host):
    browsers = bridge.installed_browsers()
    assert [b.name for b in browsers] == ["Chrome", "Chromium", "Edge", "Brave"]
    assert all(b.config.is_relative_to(host / "config") for b in browsers)
    paths = bridge.register()
    assert len(paths) == 8
    for path in paths:
        manifest = json.loads(path.read_text())
        assert manifest["allowed_origins"] == [companion.ORIGIN]
        assert manifest["type"] == "stdio" and manifest["name"] == path.stem
        executable = Path(manifest["path"])
        assert executable.is_relative_to(host / "data/cassini/native-hosts/com.gocassini.Viewer.Devel")
        assert stat.S_IMODE(executable.stat().st_mode) == 0o700
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        assert "@APPLICATION@" not in executable.read_text()
    assert not Path("SHOULD-NOT-EXIST").exists()
    assert bridge.register() == paths
    assert not list(host.rglob(".cassini-*"))


def test_actual_host_stub_keeps_binary_native_frames_and_selects_its_deployment(host):
    paths = bridge.register()
    data = bytes(range(256)) * 100 + b"\x00\x00\x00\xff"
    for path in paths[:2]:
        manifest = json.loads(path.read_text())
        output = subprocess.run([manifest["path"]], input=data, capture_output=True, check=True, timeout=5)
        assert output.stdout == data and not output.stderr
        arguments = json.loads((host / "arguments.json").read_text())
        assert arguments[:4] == ["run", "--unshare=network", "--arch=x86_64", "--branch=master"]
        assert arguments[-1] == "com.gocassini.Viewer.Devel"
        command = "cassini-browser-control-host" if manifest["name"].endswith(".control") else "cassini-browser-host"
        assert arguments[4] == "--command=" + command


def test_host_stub_passes_the_actual_browser_pid_and_start_tick_across_reconnects(host):
    path = bridge.register()[1]
    executable = json.loads(path.read_text())["path"]
    browser = host / "bin/chrome"
    shutil.copyfile(sys.executable, browser)
    browser.chmod(0o700)
    script = """import json,subprocess,sys
from pathlib import Path
arguments=[]
for _ in range(2):
    subprocess.run([sys.argv[1]],input=b'',check=True)
    arguments.append(json.loads(Path(sys.argv[2]).read_text()))
print(json.dumps(arguments))
"""
    result = subprocess.run(
        [str(browser), "-c", script, executable, str(host / "arguments.json")],
        capture_output=True,
        check=True,
        timeout=10,
    )
    first, second = json.loads(result.stdout)
    assert first == second
    assert "--env=CASSINI_HOST_BROWSER_NAME=Chrome" in first
    assert any(a.startswith("--env=CASSINI_HOST_BROWSER_INSTANCE=") and a.rsplit("=", 1)[1] for a in first)


def test_deployment_requires_effective_permission_and_known_app(tmp_path):
    path = tmp_path / "flatpak-info"
    template = "[Application]\nname={}\n[Instance]\narch=x86_64\nbranch=master\n[Session Bus Policy]\norg.freedesktop.Flatpak={}\n"
    for application in sorted(bridge.APPLICATIONS):
        path.write_text(template.format(application, "talk"))
        assert bridge.deployment(path) == bridge.Deployment(application, "x86_64", "master")
    for application, permission in (("other.App", "talk"), ("com.gocassini.Viewer", "none")):
        path.write_text(template.format(application, permission))
        with pytest.raises(OSError, match="unavailable"):
            bridge.deployment(path)


def test_missing_host_service_disables_companion(monkeypatch):
    monkeypatch.setenv("FLATPAK_ID", "com.gocassini.Viewer")
    monkeypatch.setattr(bridge, "deployment", lambda: bridge.Deployment("com.gocassini.Viewer", "x86_64", "master"))

    def unreachable():
        raise OSError("Private session has no host service")

    monkeypatch.setattr(bridge, "host_info", unreachable)
    assert not companion.available()
    monkeypatch.setattr(bridge, "host_info", lambda: None)
    assert companion.available(), "A transient host failure must be retried"


def test_sandbox_add_uses_host_browser_without_sandbox_environment_overrides(monkeypatch, tmp_path):
    monkeypatch.setenv("FLATPAK_ID", "com.gocassini.Viewer")
    monkeypatch.setattr(bridge.shutil, "which", lambda _: "/usr/bin/flatpak-spawn")
    calls = []
    companion.open_add(
        companion.Browser("Chrome", "/host/google-chrome", tmp_path), launch=lambda *a, **kw: calls.append((a, kw))
    )
    ((args, options),) = calls
    assert args[0][:3] == ["/usr/bin/flatpak-spawn", "--host", "--directory=/"]
    assert args[0][-2:] == ["/host/google-chrome", companion.ADD_URL]
    assert args[0][3:5] == ["/bin/sh", "-c"] and args[0][5].endswith(" &")
    assert "env" not in options and "shell" not in options


def test_flatpak_broker_is_shared_by_its_hosts_but_separate_from_native_and_devel(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.delenv("FLATPAK_ID", raising=False)
    native = service_path()
    monkeypatch.setenv("FLATPAK_ID", "com.gocassini.Viewer")
    regular = service_path()
    assert regular == tmp_path / "app/com.gocassini.Viewer/cassini/browser-v2.sock"
    monkeypatch.setenv("FLATPAK_ID", "com.gocassini.Viewer.Devel")
    assert len({native, regular, service_path()}) == 3


def test_only_flatpak_accepts_valid_host_browser_process_hint(monkeypatch):
    monkeypatch.setenv("CASSINI_HOST_BROWSER_NAME", "Brave")
    monkeypatch.setenv("CASSINI_HOST_BROWSER_INSTANCE", "1234:5678")
    monkeypatch.setenv("FLATPAK_ID", "com.gocassini.Viewer")
    assert parent_browser_info() == dict(browserName="Brave", browserInstance="1234:5678")
    for invalid in ("", "1:bad", "1:2\n", "1" * 201):
        monkeypatch.setenv("CASSINI_HOST_BROWSER_INSTANCE", invalid)
        assert parent_browser_info().get("browserInstance") != invalid
    monkeypatch.delenv("FLATPAK_ID")
    monkeypatch.setenv("CASSINI_HOST_BROWSER_INSTANCE", "1234:5678")
    assert parent_browser_info() != dict(browserName="Brave", browserInstance="1234:5678")


def test_both_flatpak_recipes_add_only_the_approved_bus_permission():
    root = Path(__file__).parents[1]
    for application in bridge.APPLICATIONS:
        recipe = json.loads((root / "build-aux/flatpak" / (application + ".json")).read_text())
        args = recipe["finish-args"]
        assert args == [
            "--share=ipc",
            "--share=network",
            "--socket=fallback-x11",
            "--socket=wayland",
            "--device=dri",
            "--socket=pulseaudio",
            "--filesystem=xdg-music/Cassini:create",
            "--talk-name=org.freedesktop.Flatpak",
        ]


def test_idle_broker_keeps_a_live_application_lease_then_exits(tmp_path):
    from cassini_viewer.browser_service import BrowserService, connect, read_packet, send_packet

    service = BrowserService(tmp_path / "settings.json")
    path = tmp_path / "broker/socket"
    ready = threading.Event()
    thread = threading.Thread(target=service.serve, args=(path, ready), kwargs={"idle_timeout": 0.1}, daemon=True)
    thread.start()
    assert ready.wait(2)
    connection = connect(path, timeout=1)
    try:
        with connection.makefile("rwb") as stream:
            send_packet(stream, dict(role="application", active=True, generation=1))
            assert read_packet(stream) == dict(ready=True, protocol=2)
        thread.join(0.8)
        assert thread.is_alive(), "A live application must keep its broker"
    finally:
        connection.close()
        thread.join(2)
        if thread.is_alive():
            service.closed.set()
            thread.join(2)
            pytest.fail("Idle broker did not exit after the last lease closed")
    assert not path.exists()


def test_dedicated_broker_launch_returns_before_its_host_child_finishes(host):
    # A realistic long-lived broker must not keep flatpak-spawn (and therefore
    # the original UI/host sandbox) alive after the short host shell returns.
    fake = host / "bin/flatpak"
    fake.write_text(
        '#!/bin/sh\nwhile [ ! -f "$HOME/release" ] && [ -d "$HOME" ]; do sleep .01; done\n'
        'if [ -d "$HOME" ]; then printf done > "$HOME/broker-finished"; fi\n'
    )
    fake.chmod(0o700)
    command = bridge.broker_command("/run/user/1000/app/com.gocassini.Viewer.Devel/cassini/browser-v2.sock")
    assert "--unshare=network" in command
    try:
        completed = subprocess.run(command, capture_output=True, check=True, timeout=2)
        assert completed.stdout == b"" and completed.stderr == b""
        assert not (host / "broker-finished").exists()
    finally:
        (host / "release").touch()
    deadline = time.monotonic() + 3
    while not (host / "broker-finished").exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert (host / "broker-finished").read_text() == "done"
