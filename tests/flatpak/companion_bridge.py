#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Installed Flatpak: real host launch, registration and independent native pipes.

Every host command runs on a new private bus with private HOME/XDG directories.
No extra filesystem or D-Bus grant is passed to the installed application.
"""

import argparse
import array
import base64
import json
import math
import os
import selectors
import shlex
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path


def worker(application, private, report):
    assert str(private).startswith("/tmp/cassini-flatpak-bridge-")
    assert Path(os.environ["HOME"]) == private / "home" and not os.environ.get("WAYLAND_DISPLAY")
    base = [
        "flatpak",
        "run",
        "--user",
        "--no-documents-portal",
        "--unshare=network",
        "--unshare=ipc",
        "--nosocket=wayland",
        "--nosocket=x11",
        "--nosocket=fallback-x11",
        "--nosocket=pulseaudio",
        "--command=python3",
        application,
        "-I",
        "-c",
    ]
    prefix = "import sys;sys.path.insert(0,'/app/share/cassini-viewer');"

    def until(check):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            value = check()
            if value:
                return value
            time.sleep(0.05)
        raise AssertionError("Installed bridge did not reach the expected state")

    def inside(code, *, deny=False):
        command = base
        if deny:
            split = base.index("--command=python3")
            command = base[:split] + ["--no-talk-name=org.freedesktop.Flatpak"] + base[split:]
        try:
            result = subprocess.run(
                command + [prefix + "import faulthandler;faulthandler.dump_traceback_later(15);" + code],
                capture_output=True,
                text=True,
                timeout=20,
            )
        except subprocess.TimeoutExpired as error:
            print(f"Sandbox timeout: stdout={error.stdout!r} stderr={error.stderr!r}", file=sys.stderr)
            raise
        with (report.parent / "flatpak-sandbox.log").open("a") as log:
            log.write(result.stderr)
        if result.returncode:
            print(result.stderr, file=sys.stderr)
        result.check_returncode()
        return json.loads(result.stdout)

    command = private / "bin/google-chrome"
    command.parent.mkdir()
    command.write_text(
        f'#!/bin/sh\nprintf "%s\\n" "$@" > "{private}/add-url.txt"\n'
        'while [ ! -f "$HOME/release-browser" ] && [ -d "$HOME" ]; do sleep .01; done\n'
    )
    command.chmod(0o700)
    for name in ("chromium", "microsoft-edge", "brave-browser"):
        (command.parent / name).symlink_to(command.name)
    # D27: exercise cleanup from the installed sandbox, against the host's
    # private config. Matching origins alone must never delete another app.
    legacy = private / "config/chromium/NativeMessagingHosts/com.codemyriad.cassini.json"
    legacy.parent.mkdir(parents=True)
    owned = json.dumps(dict(name=legacy.stem, path="/old/cassini-browser-host", allowed_origins=[]))
    legacy.write_text(owned)
    foreign = legacy.with_name("com.codemyriad.cassini.control.json")
    foreign_bytes = json.dumps(
        dict(
            name=foreign.stem,
            path="/foreign/other-host",
            allowed_origins=["chrome-extension://pcafdaahfhjiolfpfnlhmljfgoabbaij/"],
        )
    ).encode()
    foreign.write_bytes(foreign_bytes)
    assert inside(
        "import json;from cassini_viewer.companion import start_legacy_cleanup;"
        "thread=start_legacy_cleanup();thread.join(10);print(json.dumps(not thread.is_alive()))"
    )
    assert not legacy.exists() and foreign.read_bytes() == foreign_bytes
    legacy.write_text(owned)  # Connect must perform the same host-side cleanup.
    # Session-helper inherits this host PATH, not the sandbox's /app/bin PATH.
    # Activation happens only after these private executables are in place.
    info = inside(
        "import json,os;from cassini_viewer import companion,flatpak_bridge;"
        "flatpak_bridge.deployment();flatpak_bridge.host_info();"
        "assert companion.available();browsers=companion.installed_browsers();"
        "written=companion.register();child=companion.open_add(browsers[0]);assert child.wait(5)==0;"
        "print(json.dumps(dict(browsers=[b.name for b in browsers],paths=[str(p) for p in written],"
        "sandboxConfig=os.environ['XDG_CONFIG_HOME'])))"
    )
    assert info["browsers"] == ["Chrome", "Chromium", "Edge", "Brave"]
    assert not legacy.exists() and foreign.read_bytes() == foreign_bytes
    assert info["sandboxConfig"] != os.environ["XDG_CONFIG_HOME"]
    until(lambda: (private / "add-url.txt").exists())
    assert (private / "add-url.txt").read_text().strip() == (
        "https://chromewebstore.google.com/detail/pcafdaahfhjiolfpfnlhmljfgoabbaij"
    )
    (private / "home/release-browser").touch()
    assert not inside(
        "import json;from cassini_viewer import companion;print(json.dumps(companion.available()))", deny=True
    ), "An effective permission override must retain the unusable-bridge gate"
    assert len(info["paths"]) == 8
    hosts = {}
    for name in info["paths"]:
        path = Path(name)
        assert path.is_relative_to(private / "config")
        manifest = json.loads(path.read_text())
        assert manifest["allowed_origins"] == ["chrome-extension://pcafdaahfhjiolfpfnlhmljfgoabbaij/"]
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        executable = Path(manifest["path"])
        assert executable.is_relative_to(private / "data/cassini/native-hosts" / application)
        assert stat.S_IMODE(executable.stat().st_mode) == 0o700
        hosts[manifest["name"]] = str(executable)

    def status():
        return inside(
            "import json;from cassini_viewer.browser_service import request,ensure_service;"
            "print(json.dumps(request(path=ensure_service())))"
        )

    def send(process, message):
        data = json.dumps(message).encode()
        process.stdin.write(struct.pack("<I", len(data)) + data)
        process.stdin.flush()

    def receive(process):
        def exact(size):
            data = b""
            deadline = time.monotonic() + 15
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while len(data) < size:
                    assert selector.select(max(0, deadline - time.monotonic())), "Native pipe timed out"
                    part = os.read(process.stdout.fileno(), size - len(data))
                    assert part, "Native pipe closed before a complete reply"
                    data += part
            return data

        (size,) = struct.unpack("<I", exact(4))
        assert 0 < size <= 1024 * 1024
        return json.loads(exact(size))

    processes, logs = [], []

    def launch(name):
        log = (report.parent / f"flatpak-{len(processes)}-host.log").open("wb")
        logs.append(log)
        process = subprocess.Popen([hosts[name]], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log, bufsize=0)
        processes.append(process)
        return process

    try:
        control = launch("io.codemyriad.cassini.control")
        send(control, dict(type="hello", version=1, capabilities=["independent-sessions-v1"]))
        send(control, dict(type="status", recording=False, tabId=None))
        send(control, dict(type="meetings", meetings=[]))
        until(lambda: status()["connectedBrowsers"] == 1)
        directories, expected = [], {}
        for index, frequency in enumerate((880, 990)):
            audio = launch("io.codemyriad.cassini")
            send(
                audio,
                dict(
                    type="start",
                    version=1,
                    independent=True,
                    captureId=f"bridge-{index}",
                    timelineOriginMs=int(time.time() * 1000),
                    microphone=False,
                    title=f"Private bridge {index}",
                ),
            )
            reply = receive(audio)
            assert reply["type"] == "started", reply
            directory = Path(reply["path"])
            assert directory.parent == private / "Musica/Cassini"
            directories.append(directory)
            send(
                audio,
                dict(type="track", id="remote", label="Synthetic remote", kind="remote", sampleRate=48000, channels=1),
            )
            assert receive(audio)["type"] == "ack"
            pcm = array.array("h", (int(6500 * math.sin(2 * math.pi * frequency * i / 48000)) for i in range(96000)))
            if sys.byteorder != "little":
                pcm.byteswap()
            data = pcm.tobytes()
            expected[directory] = data
            for sequence in range(20):
                send(
                    audio,
                    dict(
                        type="audio",
                        id="remote",
                        sequence=sequence,
                        offsetMs=sequence * 100,
                        pcm=base64.b64encode(data[sequence * 9600 : (sequence + 1) * 9600]).decode(),
                    ),
                )
                assert receive(audio)["type"] == "ack"
        assert directories[0] != directories[1]
        snapshot = status()
        assert sum(s["recording"] for s in snapshot["statuses"]) == 2
        # Stop the first pipe while the second remains live in the same broker.
        for index, audio in enumerate(processes[1:]):
            send(audio, dict(type="stop"))
            assert receive(audio)["type"] == "stopped"
            audio.stdin.close()
            assert audio.wait(10) == 0
            if index == 0:
                assert sum(s["recording"] for s in status()["statuses"]) == 1
        control.stdin.close()
        assert control.wait(10) == 0
        for directory, data in expected.items():
            manifest = json.loads((directory / "session.json").read_text())
            assert manifest["state"] == "completed" and len(manifest["tracks"]) == 1
            (track,) = manifest["tracks"]
            assert track["frames"] == 96000 and track["sourceTrackId"] == "remote"
            with wave.open(str(directory / track["file"]), "rb") as stream:
                assert stream.readframes(96000) == data
        assert inside(
            "import json,time;from cassini_viewer.browser_service import service_path;"
            "path=service_path();deadline=time.monotonic()+15;"
            "exec('while path.exists() and time.monotonic()<deadline: time.sleep(.05)');"
            "print(json.dumps(not path.exists()))"
        ), "Dedicated broker must exit after its last client becomes idle"
        report.write_text(
            json.dumps(
                dict(
                    installedManifestPermission=True,
                    extraGrants=False,
                    hostConfiguration=True,
                    sandboxConfiguration=info["sandboxConfig"],
                    addURL=True,
                    registeredHosts=8,
                    connectedHandshake=True,
                    detachedAdd=True,
                    deniedBridgeGated=True,
                    idleExit=True,
                    independentPipes=2,
                    exactPCMFramesPerPipe=96000,
                    completed=True,
                    individualStop=True,
                ),
                indent=2,
            )
            + "\n"
        )
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(5)
        # Removing only this private broker's directory releases its serve loop.
        inside(
            "import shutil,json;from cassini_viewer.browser_service import service_path;"
            "shutil.rmtree(service_path().parent,ignore_errors=True);print(json.dumps(True))"
        )
        for log in logs:
            log.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("application")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--worker", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        worker(args.application, args.worker, args.output)
        return
    permissions = subprocess.check_output(
        ["flatpak", "info", "--user", "--show-permissions", args.application], text=True
    )
    assert "org.freedesktop.Flatpak=talk" in permissions, "Installed manifest lacks the bridge permission"
    assert "filesystems=xdg-music/Cassini:create;" in permissions
    assert "filesystems=home" not in permissions and ";home" not in permissions
    location = subprocess.check_output(
        ["flatpak", "info", "--user", "--show-location", args.application], text=True
    ).strip()
    repository = location.split("/app/")[0]
    assert repository != location
    args.output = args.output.resolve()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="cassini-flatpak-bridge-") as temporary:
        private = Path(temporary)
        for name in ("home", "config", "data", "cache", "runtime", "Musica"):
            (private / name).mkdir(mode=0o700)
        (private / "config/user-dirs.dirs").write_text(f'XDG_MUSIC_DIR="{private}/Musica"\n')
        # The official build image has the real Flatpak helper under /app but
        # omits its activation file. Supply it only on this private session bus.
        prefix = Path(shutil.which("flatpak")).resolve().parents[1]
        helper = next(
            (
                path
                for path in (
                    prefix / "libexec/flatpak-session-helper",
                    prefix / "lib/flatpak-session-helper",
                    prefix / "lib/flatpak/flatpak-session-helper",
                )
                if path.is_file() and os.access(path, os.X_OK)
            ),
            None,
        )
        assert helper is not None, "Install the Flatpak session helper for the private bridge test"
        services = private / "data/dbus-1/services"
        services.mkdir(parents=True)
        (services / "org.freedesktop.Flatpak.service").write_text(
            "[D-BUS Service]\nName=org.freedesktop.Flatpak\nExec=" + shlex.quote(str(helper)) + "\n"
        )
        env = dict(
            os.environ,
            HOME=str(private / "home"),
            XDG_CONFIG_HOME=str(private / "config"),
            XDG_CACHE_HOME=str(private / "cache"),
            XDG_DATA_HOME=str(private / "data"),
            XDG_RUNTIME_DIR=str(private / "runtime"),
            FLATPAK_USER_DIR=repository,
            PATH=str(private / "bin") + ":" + os.environ["PATH"],
            PULSE_SERVER="unix:" + str(private / "no-audio.sock"),
        )
        for key in ("WAYLAND_DISPLAY", "WAYLAND_SOCKET", "DISPLAY", "DBUS_SESSION_BUS_ADDRESS", "CASSINI_LIBRARY_DIR"):
            env.pop(key, None)
        subprocess.run(
            [
                "dbus-run-session",
                "--dbus-daemon=" + (shutil.which("dbus-daemon") or "dbus-daemon"),
                "--",
                sys.executable,
                str(Path(__file__).resolve()),
                args.application,
                "--output",
                str(args.output),
                "--worker",
                temporary,
            ],
            env=env,
            check=True,
            timeout=120,
        )
    print(args.output.read_text())


if __name__ == "__main__":
    main()
