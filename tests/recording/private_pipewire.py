# SPDX-License-Identifier: GPL-3.0-or-later
"""Private timerless microphone; never discover or open desktop audio devices."""

import os
import subprocess
import tempfile
from pathlib import Path

from readiness import wait_until


class PrivatePipeWire:
    def __init__(self, artifacts):
        if os.environ.get("CASSINI_ISOLATED_AUDIO") != "1" or os.environ.get("WAYLAND_DISPLAY"):
            raise RuntimeError("Use the isolated recording harness")
        self.children, self.logs = [], []
        self.previous = os.environ.get("PULSE_SERVER")
        self.temporary = tempfile.TemporaryDirectory(prefix="cassini-pw-")
        self.runtime = Path(self.temporary.name)
        self.output = Path(artifacts)
        config = self.output / "pipewire-config"
        directory = config / "pipewire/pipewire.conf.d"
        directory.mkdir(parents=True)
        (directory / "90-cassini-no-frames.conf").write_text(
            "context.spa-libs = { audiotestsrc = audiotestsrc/libspa-audiotestsrc }\n"
            "context.objects = [{ factory = adapter args = { factory.name = audiotestsrc "
            'node.name = mic node.description = "No-frame test microphone" media.class = Audio/Source '
            "audio.position = [ MONO ] audio.rate = 48000 node.param.Props = { live = false } } }]\n"
        )
        directory = config / "wireplumber/wireplumber.conf.d"
        directory.mkdir(parents=True)
        (directory / "90-no-devices.conf").write_text(
            "wireplumber.profiles = { main = { monitor.alsa = disabled monitor.bluez = disabled "
            "monitor.v4l2 = disabled } }\n"
        )
        self.environment = dict(os.environ, XDG_RUNTIME_DIR=str(self.runtime), XDG_CONFIG_HOME=str(config))
        self.environment.pop("PULSE_SERVER", None)
        try:
            self._start("pipewire")
            wait_until(lambda: (self.runtime / "pipewire-0").exists(), "private PipeWire socket")
            self._start("wireplumber")
            self._start("pipewire-pulse")
            os.environ["PULSE_SERVER"] = "unix:" + str(self.runtime / "pulse/native")
            wait_until(lambda: self._control("info"), "private PipeWire Pulse connection")
            subprocess.run(
                ["pactl", "load-module", "module-null-sink", "sink_name=speakers", "rate=48000", "channels=1"],
                check=True,
                capture_output=True,
                timeout=5,
            )
            wait_until(lambda: self._control("set-default-source", "mic"), "private no-frame microphone")
            wait_until(lambda: self._control("set-default-sink", "speakers"), "private speaker sink")
        except BaseException:
            self.close()
            raise

    def _start(self, name):
        log = (self.output / (name + ".log")).open("w")
        self.logs.append(log)
        self.children.append(subprocess.Popen([name], env=self.environment, stdout=log, stderr=log))

    @staticmethod
    def _control(*arguments):
        return subprocess.run(["pactl", *arguments], capture_output=True, timeout=2).returncode == 0

    def close(self):
        for child in reversed(self.children):
            child.terminate()
        for child in self.children:
            try:
                child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=3)
        for log in self.logs:
            log.close()
        self.temporary.cleanup()
        if self.previous is None:
            os.environ.pop("PULSE_SERVER", None)
        else:
            os.environ["PULSE_SERVER"] = self.previous
