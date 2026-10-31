# SPDX-License-Identifier: GPL-3.0-or-later
"""Private Pulse/DBus helpers for real installed identity-upgrade journeys."""

import array
import json
import math
import os
import subprocess
import time
import wave
from pathlib import Path

import gi

gi.require_version("Atspi", "2.0")
from gi.repository import Atspi, Gio, GLib  # noqa: E402


def until(check, label, timeout=25):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        context = GLib.MainContext.default()
        for _ in range(64):
            if not context.pending():
                break
            context.iteration(False)
        try:
            result = check()
            if result:
                return result
        except (GLib.Error, OSError, ValueError):
            pass
        time.sleep(0.02)
    raise AssertionError("Timed out: " + label)


class Audio:
    def __init__(self, output):
        directory = Path(output) / "private-audio"
        directory.mkdir(mode=0o700)
        runtime = directory / "runtime"
        runtime.mkdir(mode=0o700)
        self.env = dict(
            os.environ,
            HOME=str(directory),
            XDG_CONFIG_HOME=str(directory / "config"),
            XDG_DATA_HOME=str(directory / "data"),
            XDG_CACHE_HOME=str(directory / "cache"),
            PULSE_RUNTIME_PATH=str(runtime),
            XDG_RUNTIME_DIR=str(runtime),
        )
        self.uid = 1000 if os.getuid() == 0 else os.getuid()
        self.gid = 1000 if os.getuid() == 0 else os.getgid()
        if os.getuid() == 0:
            os.chown(directory, self.uid, self.gid)
            os.chown(runtime, self.uid, self.gid)
        config = directory / "server.pa"
        socket = directory / "pulse.sock"
        config.write_text(
            f"load-module module-native-protocol-unix socket={socket} auth-anonymous=1\n"
            "load-module module-null-sink sink_name=cassini_microphone rate=48000 channels=1\n"
            "load-module module-null-sink sink_name=cassini_desktop rate=48000 channels=2\n"
            "set-default-sink cassini_desktop\nset-default-source cassini_microphone.monitor\n"
        )
        subprocess.run(
            [
                "pulseaudio",
                "-n",
                "--daemonize",
                "--exit-idle-time=-1",
                "--file=" + str(config),
                "--log-target=file:" + str(directory / "pulse.log"),
            ],
            env=self.env,
            user=self.uid,
            group=self.gid,
            check=True,
        )
        until(socket.exists, "private Pulse socket")
        os.environ["PULSE_SERVER"] = "unix:" + str(socket)
        os.environ["CASSINI_ISOLATED_AUDIO"] = "1"
        tone = directory / "tone.wav"
        with wave.open(str(tone), "wb") as audio:
            audio.setparams((1, 2, 48000, 0, "NONE", "not compressed"))
            second = array.array("h", [int(10000 * math.sin(2 * math.pi * 660 * n / 48000)) for n in range(48000)])
            for _ in range(90):
                audio.writeframes(second.tobytes())
        self.producers = [
            subprocess.Popen(["paplay", "--device=" + sink, str(tone)])
            for sink in ("cassini_microphone", "cassini_desktop")
        ]

    def close(self):
        for producer in self.producers:
            producer.terminate()
            producer.wait(timeout=5)
        subprocess.run(["pulseaudio", "--kill"], env=self.env, user=self.uid, group=self.gid, check=False)


class Live:
    def __init__(self, output):
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.output = Path(output)
        self.children = []
        self.logs = []

    def owner(self, app):
        return self.bus.call_sync(
            "org.freedesktop.DBus",
            "/org/freedesktop/DBus",
            "org.freedesktop.DBus",
            "NameHasOwner",
            GLib.Variant("(s)", (app,)),
            None,
            Gio.DBusCallFlags.NONE,
            1000,
            None,
        ).unpack()[0]

    def action(self, app, action):
        self.bus.call_sync(
            app,
            "/" + app.replace(".", "/"),
            "org.gtk.Actions",
            "Activate",
            GLib.Variant("(sava{sv})", (action, [], {})),
            None,
            Gio.DBusCallFlags.NONE,
            2000,
            None,
        )

    def launch(self, executable, name):
        log = (self.output / (name + ".log")).open("w")
        self.logs.append(log)
        child = subprocess.Popen([str(executable)], stdout=log, stderr=log)
        self.children.append(child)
        return child

    def visible(self, child):
        return (
            subprocess.run(
                ["xdotool", "search", "--onlyvisible", "--pid", str(child.pid)], capture_output=True, timeout=2
            ).returncode
            == 0
        )

    def recording(self, root):
        for folder in (*Path(root).glob("Recording-*"), *Path(root).glob(".cassini/sessions/Recording-*")):
            manifest = json.loads((folder / "session.json").read_text())
            if manifest["state"] == "recording" and self.samples(folder) >= 48000:
                return folder
        return None

    def samples(self, folder):
        value = json.loads((Path(folder) / "session.json").read_text())
        return min(((Path(folder) / t["file"]).stat().st_size - 44) // (2 * t["channels"]) for t in value["tracks"])

    def saved(self, folder):
        manifest = json.loads((Path(folder) / "session.json").read_text())
        # Device-clock gaps are explicitly kept as silence by the format.
        # They are a saved recording with a warning, never an unfinished file.
        terminal = manifest["state"] == "completed" or (
            manifest["state"] == "interrupted" and manifest.get("problemKind") == "gap"
        )
        return terminal and all(track.get("ended") for track in manifest["tracks"])

    def quit_legacy(self, app, child):
        self.action(app, "quit")
        confirmed = False

        def walk(node):
            yield node
            for index in range(node.get_child_count()):
                item = node.get_child_at_index(index)
                if item is not None:
                    yield from walk(item)

        def exited():
            nonlocal confirmed
            if child.poll() is not None:
                return True
            if not confirmed:
                for node in walk(Atspi.get_desktop(0)):
                    if (
                        node.get_process_id() == child.pid
                        and node.get_name() == "Stop and keep it"
                        and node.get_state_set().contains(Atspi.StateType.SHOWING)
                    ):
                        assert node.get_action_iface().do_action(0)
                        confirmed = True
                        break
            return False

        until(exited, "released app exits through its real Stop-and-keep confirmation")
        assert child.returncode == 0

    def close(self):
        for child in self.children:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=10)
        for log in self.logs:
            log.close()
