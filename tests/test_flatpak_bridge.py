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
        f"if sys.argv[1]=='list': print('io.codemyriad.CassiniTalk.Devel x86_64 master' if Path({str(directory / 'deployment')!r}).exists() else '');sys.exit(0)\n"
        f"if sys.argv[1]=='info': print({str(directory / 'deployment')!r});sys.exit(0)\n"
        f"Path({str(directory / 'arguments.json')!r}).write_text(json.dumps(sys.argv[1:]))\n"
        "sys.stdout.buffer.write(sys.stdin.buffer.read())\n"
    )
    fake.chmod(0o700)
    deployed = directory / "deployment/files/bin"
    deployed.mkdir(parents=True)
    for name in ("cassini-browser-host", "cassini-browser-control-host"):
        path = deployed / name
        path.write_text("#!/bin/sh\ncat\n")
        path.chmod(0o700)
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
        bridge, "deployment", lambda: bridge.Deployment("io.codemyriad.CassiniTalk.Devel", "x86_64", "master")
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
        assert executable.is_relative_to(host / "data/cassini/native-hosts/io.codemyriad.CassiniTalk.Devel")
        assert stat.S_IMODE(executable.stat().st_mode) == 0o700
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        assert "@APPLICATION@" not in executable.read_text()
    assert not Path("SHOULD-NOT-EXIST").exists()
    originals = {p: (p.stat().st_mtime_ns, p.stat().st_ino) for p in paths}
    stubs = {Path(json.loads(p.read_text())["path"]) for p in paths}
    stub_originals = {p: (p.stat().st_mtime_ns, p.stat().st_ino) for p in stubs}
    assert bridge.register() == paths
    assert originals == {p: (p.stat().st_mtime_ns, p.stat().st_ino) for p in paths}
    assert stub_originals == {p: (p.stat().st_mtime_ns, p.stat().st_ino) for p in stubs}
    assert not [p for p in host.rglob(".cassini-*") if p.name != ".cassini-registration.lock"]


@pytest.mark.parametrize(
    ("failure", "expected"), [("permission", (False, False)), ("host", (False, True)), (None, (True, False))]
)
def test_probe_retries_only_a_recoverable_host_failure(monkeypatch, failure, expected):
    calls = []

    def deployment():
        if failure == "permission":
            raise OSError("Sandbox host access is not granted")
        return bridge.Deployment("io.codemyriad.CassiniTalk", "x86_64", "master")

    def host_info():
        calls.append(1)
        if failure == "host":
            raise OSError("Transient host timeout")

    monkeypatch.setattr(bridge, "deployment", deployment)
    monkeypatch.setattr(bridge, "host_info", host_info)
    assert bridge.probe() == expected
    assert len(calls) == (0 if failure == "permission" else 1)


def test_host_profile_inventory_observes_new_profiles_without_reading_contents(host):
    browsers = bridge.installed_browsers()
    initial = bridge.profile_inventory(browsers)
    profile = browsers[0].config / "Default"
    profile.mkdir(parents=True)
    seen = bridge.profile_inventory(browsers)
    assert seen != initial and "Default" in seen
    (profile / "Preferences").write_text("irrelevant profile content")
    assert bridge.profile_inventory(browsers) == seen
    (profile.parent / "Profile 2").mkdir()
    assert bridge.profile_inventory(browsers) != seen


def test_unsafe_host_manifest_is_preserved_and_other_profiles_still_register(host):
    browsers = bridge.installed_browsers()
    directory = browsers[0].config / "NativeMessagingHosts"
    directory.mkdir(parents=True)
    foreign = host / "foreign.json"
    foreign.write_text("another application's data")
    link = directory / "io.codemyriad.cassini.json"
    link.symlink_to(foreign)
    with pytest.raises(companion.SetupError, match="unsafe"):
        bridge.register()
    assert link.is_symlink() and foreign.read_text() == "another application's data"
    for browser in browsers[1:]:
        assert len(list((browser.config / "NativeMessagingHosts").glob("*.json"))) == 2


def test_actual_host_stub_keeps_binary_native_frames_and_selects_its_deployment(host):
    paths = bridge.register()
    data = bytes(range(256)) * 100 + b"\x00\x00\x00\xff"
    for path in paths[:2]:
        manifest = json.loads(path.read_text())
        output = subprocess.run([manifest["path"]], input=data, capture_output=True, check=True, timeout=5)
        assert output.stdout == data and not output.stderr
        arguments = json.loads((host / "arguments.json").read_text())
        assert arguments[:4] == ["run", "--unshare=network", "--arch=x86_64", "--branch=master"]
        assert arguments[-1] == "io.codemyriad.CassiniTalk.Devel"
        command = "cassini-browser-control-host" if manifest["name"].endswith(".control") else "cassini-browser-host"
        assert arguments[4] == "--command=" + command


def test_legacy_cleanup_reads_checks_and_deletes_only_on_host(host, monkeypatch):
    from test_host_migration import OLD, write_legacy

    directory = host / "config/chromium/NativeMessagingHosts"
    owned = write_legacy(directory)
    foreign = write_legacy(directory, OLD + ".control", "not-cassini")
    before = foreign.read_bytes()
    calls = []
    original = Path.open

    def forbid_local_read(path, *args, **kwargs):
        assert not path.is_relative_to(host / "config"), "Sandbox read a host manifest directly"
        return original(path, *args, **kwargs)

    def host_command(command, **kwargs):
        calls.append((command, kwargs))
        return command

    with monkeypatch.context() as patch:
        patch.setattr(bridge, "host_command", host_command)
        patch.setattr(Path, "open", forbid_local_read)
        assert bridge.cleanup_legacy() == [owned]
    assert not owned.exists() and foreign.read_bytes() == before
    assert any(bridge.READ_LEGACY in args for args, _ in calls)
    assert any(bridge.DELETE_LEGACY in args for args, _ in calls)
    assert all(kw.get("watch") is True for _, kw in calls)


def test_host_cleanup_compares_original_bytes_before_deleting(host, monkeypatch):
    from test_host_migration import write_legacy

    path = write_legacy(host / "config/chromium/NativeMessagingHosts")
    original = subprocess.run
    replacement = b'{"name":"someone.else","path":"/other/app"}'

    def changed(command, **kwargs):
        if bridge.DELETE_LEGACY in command:
            path.write_bytes(replacement)
        return original(command, **kwargs)

    monkeypatch.setattr(bridge.subprocess, "run", changed)
    assert bridge.cleanup_legacy() == []
    assert path.read_bytes() == replacement


def test_connect_removes_owned_legacy_files_after_publishing_new_hosts(host):
    from test_host_migration import write_legacy

    path = write_legacy(host / "config/chromium/NativeMessagingHosts")
    written = bridge.register()
    assert len(written) == 8 and not path.exists()
    assert all(p.name.startswith("io.codemyriad.cassini") for p in written)


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
    for application, permission in (("other.App", "talk"), ("io.codemyriad.CassiniTalk", "none")):
        path.write_text(template.format(application, permission))
        with pytest.raises(OSError, match="unavailable"):
            bridge.deployment(path)


def test_missing_host_service_disables_companion(monkeypatch):
    monkeypatch.setenv("FLATPAK_ID", "io.codemyriad.CassiniTalk")
    monkeypatch.setattr(
        bridge, "deployment", lambda: bridge.Deployment("io.codemyriad.CassiniTalk", "x86_64", "master")
    )

    def unreachable():
        raise OSError("Private session has no host service")

    monkeypatch.setattr(bridge, "host_info", unreachable)
    assert not companion.available()
    monkeypatch.setattr(bridge, "host_info", lambda: None)
    assert companion.available(), "A transient host failure must be retried"


@pytest.mark.parametrize("local", [False, True])
def test_sandbox_add_uses_host_browser_without_sandbox_environment_overrides(monkeypatch, tmp_path, local):
    monkeypatch.setenv("FLATPAK_ID", "io.codemyriad.CassiniTalk")
    monkeypatch.setattr(bridge.shutil, "which", lambda _: "/usr/bin/flatpak-spawn")
    calls = []
    (companion.open_new_tab if local else companion.open_add)(
        companion.Browser("Chrome", "/host/google-chrome", tmp_path), launch=lambda *a, **kw: calls.append((a, kw))
    )
    ((args, options),) = calls
    assert args[0][:3] == ["/usr/bin/flatpak-spawn", "--host", "--directory=/"]
    assert (
        args[0][-3:] == ["/host/google-chrome", "--new-tab", "about:blank"]
        if local
        else args[0][-2:] == ["/host/google-chrome", companion.ADD_URL]
    )
    assert args[0][3:5] == ["/bin/sh", "-c"] and args[0][5] == bridge.BROWSER_LAUNCH
    assert "env" not in options and "shell" not in options


def test_flatpak_broker_is_shared_by_its_hosts_but_separate_from_native_and_devel(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.delenv("FLATPAK_ID", raising=False)
    native = service_path()
    monkeypatch.setenv("FLATPAK_ID", "io.codemyriad.CassiniTalk")
    regular = service_path()
    assert regular == tmp_path / "app/io.codemyriad.CassiniTalk/cassini/browser-v2.sock"
    monkeypatch.setenv("FLATPAK_ID", "io.codemyriad.CassiniTalk.Devel")
    assert len({native, regular, service_path()}) == 3


def test_only_flatpak_accepts_valid_host_browser_process_hint(monkeypatch):
    monkeypatch.setenv("CASSINI_HOST_BROWSER_NAME", "Brave")
    monkeypatch.setenv("CASSINI_HOST_BROWSER_INSTANCE", "1234:5678")
    monkeypatch.setenv("FLATPAK_ID", "io.codemyriad.CassiniTalk")
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
    command = bridge.broker_command("/run/user/1000/app/io.codemyriad.CassiniTalk.Devel/cassini/browser-v2.sock")
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


def test_host_manifest_comparison_needs_no_diffutils_and_preserves_trailing_newlines(host, monkeypatch):
    tools = host / "coreutils-only"
    tools.mkdir()
    for name in ("cat", "head", "mkdir", "mktemp", "rm", "chmod", "mv"):
        (tools / name).symlink_to(shutil.which(name))
    monkeypatch.setenv("PATH", str(tools))
    assert shutil.which("cmp") is None
    path = host / "manifests/own.json"
    content = '{"path":"a"}\n\n'
    bridge._write(path, content, "600")
    original = path.stat()
    bridge._write(path, content, "600")
    current = path.stat()
    assert (current.st_ino, current.st_mtime_ns) == (original.st_ino, original.st_mtime_ns)
    assert path.read_text() == content
    changed = '{"path":"b"}\n'
    bridge._write(path, changed, "600")
    assert path.read_text() == changed
    assert not list(path.parent.glob(".cassini-*"))


def test_host_flatpak_browser_detection_and_registration_use_host_home(host):
    applications = host / "data/applications"
    applications.mkdir(parents=True)
    (applications / "org.chromium.Chromium.desktop").write_text(
        "[Desktop Entry]\nExec=flatpak run org.chromium.Chromium %U\n"
    )
    browsers = bridge.installed_browsers()
    browser = next(b for b in browsers if b.arguments)
    assert browser.config == host / ".var/app/org.chromium.Chromium/config/chromium"
    paths = bridge.register(browsers=[browser])
    assert len(paths) == 2 and all(p.parent == browser.config / "NativeMessagingHosts" for p in paths)


@pytest.mark.parametrize(
    "error", [subprocess.TimeoutExpired("desktop probe", 2), subprocess.CalledProcessError(1, "desktop probe")]
)
def test_desktop_probe_process_failures_are_reported_as_io_errors(host, monkeypatch, error):
    real_run = subprocess.run

    def run(command, **kwargs):
        if "cassini-desktops" in command:
            raise error
        return real_run(command, **kwargs)

    monkeypatch.setattr(bridge.subprocess, "run", run)
    with pytest.raises(OSError, match="Could not inspect host browsers") as caught:
        bridge.installed_browsers()
    assert caught.value.__cause__ is error


@pytest.mark.parametrize("code", [0, 42])
def test_host_browser_early_exit_crosses_the_real_shell_bridge(tmp_path, monkeypatch, code):
    launcher = tmp_path / "host-browser"
    launcher.write_text(f"#!/bin/sh\nexit {code}\n")
    launcher.chmod(0o700)
    monkeypatch.setattr(companion, "in_flatpak", lambda: True)
    # Run the exact host-side shell without a real desktop or host service.
    monkeypatch.setattr(bridge, "host_command", lambda command, **_kwargs: command)
    process = companion.open_new_tab(companion.Browser("Chrome", str(launcher), tmp_path))
    if code:
        with pytest.raises(OSError, match="status 42"):
            companion.check_browser_launch(process)
    else:
        companion.check_browser_launch(process)


def test_host_browser_probe_detaches_a_live_process_without_waiting_for_its_lifetime(tmp_path, monkeypatch):
    entered, release, finished = tmp_path / "entered", tmp_path / "release", tmp_path / "finished"
    launcher = tmp_path / "host-browser"
    launcher.write_text(
        f'#!/bin/sh\n: > "{entered}"\nwhile [ ! -f "{release}" ]; do sleep .01; done\n: > "{finished}"\n'
    )
    launcher.chmod(0o700)
    monkeypatch.setattr(companion, "in_flatpak", lambda: True)
    monkeypatch.setattr(bridge, "host_command", lambda command, **_kwargs: command)
    process = companion.open_new_tab(companion.Browser("Chrome", str(launcher), tmp_path))
    try:
        companion.check_browser_launch(process)
        assert process.returncode == 0 and entered.exists() and not release.exists()
    finally:
        release.touch()
        process.wait(timeout=2)
    deadline = time.monotonic() + 2
    while not finished.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert finished.exists(), "The detached browser must survive its launcher and finish normally"
