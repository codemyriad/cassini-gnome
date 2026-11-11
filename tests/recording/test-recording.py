#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""D60 (R83) in a private real Pulse: the test recording through the real app.

Each case runs the app's own test (8 s, both native streams) and checks its
result per stream, that its private folder is gone afterwards, and that the
app saw no recording: no capture, Library row, notification or transcription.
A real Record during a test discards it and starts as usual.
"""

import argparse
import json
import logging
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import traceback
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

if not os.environ.get("CASSINI_ISOLATED_AUDIO") or os.environ.get("WAYLAND_DISPLAY"):
    raise SystemExit("Use tests/recording/run.sh")
parser = argparse.ArgumentParser()
parser.add_argument("--build", required=True)
args = parser.parse_args()
build = Path(args.build).resolve()
schema_dir = build / "recording-test-schemas"
schema_dir.mkdir(exist_ok=True)
shutil.copyfile(
    Path(__file__).resolve().parents[2] / "data/io.codemyriad.CassiniTalk.gschema.xml",
    schema_dir / "io.codemyriad.CassiniTalk.gschema.xml",
)
subprocess.run(["glib-compile-schemas", str(schema_dir)], check=True)
os.environ["GSETTINGS_SCHEMA_DIR"] = str(schema_dir)
import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gst", "1.0")
from gi.repository import Gio, Gst  # noqa: E402
from readiness import wait_until  # noqa: E402
from teardown import recording_app_teardown  # noqa: E402

sys.path[:0] = [str(Path(__file__).resolve().parents[2] / name) for name in ("src", "tests")]
from desktop_services import DesktopServices  # noqa: E402

Gio.resources_register(Gio.Resource.load(str(build / "src/cassini-talk.gresource")))
from cassini_talk.capture_activity import application_activity  # noqa: E402
from cassini_talk.library_window import LibraryWindow  # noqa: E402
from cassini_talk.main import CassiniApplication  # noqa: E402
from cassini_talk.sound_check import REFUSED, SILENT, WORKING, folders_root  # noqa: E402

artifacts = Path(os.environ["CASSINI_TEST_ARTIFACTS"])
artifacts.mkdir(parents=True, exist_ok=True)
evidence = {"passed": False, "cases": {}}
callback_errors = []


def callback_error(kind, value, tb):
    callback_errors.append("".join(traceback.format_exception(kind, value, tb)))
    sys.__excepthook__(kind, value, tb)


sys.excepthook = callback_error
# A crash left a test folder behind: the app's start sweeps it.
for level in (folders_root().parent.parent, folders_root().parent, folders_root()):
    level.mkdir(mode=0o700, exist_ok=True)
leftover = folders_root() / str(uuid4())
(leftover / "Recording-x").mkdir(mode=0o700, parents=True)
(leftover / "Recording-x/track-system.wav").write_bytes(b"left by a crash")
services = DesktopServices()
app = CassiniApplication("com.gocassini.TestRecordingTest", "test", "development")
# Recording-state hooks: none may run for a test recording.
seen = []
for name in ("start_native", "_native_started", "_native_finished", "_native_error", "write_down", "inhibit"):
    original = getattr(app, name)
    setattr(app, name, lambda *a, _name=name, _original=original, **k: seen.append(_name) or _original(*a, **k))


class Silence:
    """Consecutive digital-silence frames on a sink's monitor, thread-safely."""

    def __init__(self, device):
        self.pipeline = Gst.parse_launch(
            f"pulsesrc device={device}.monitor ! audioconvert ! audioresample ! "
            "audio/x-raw,format=S16LE,rate=48000,channels=1 ! fakesink name=observed sync=false"
        )
        self.frames, self.audible, self._lock = 0, 0, threading.Lock()
        pad = self.pipeline.get_by_name("observed").get_static_pad("sink")
        pad.add_probe(Gst.PadProbeType.BUFFER, self._buffer)
        self.pipeline.set_state(Gst.State.PLAYING)

    def _buffer(self, _pad, info):
        buffer = info.get_buffer()
        mapped, mapping = buffer.map(Gst.MapFlags.READ)
        if mapped:
            try:
                loud = any(mapping.data)
            finally:
                buffer.unmap(mapping)
            with self._lock:
                frames = buffer.get_size() // 2
                self.frames = 0 if loud else self.frames + frames
                self.audible = self.audible + frames if loud else 0
        return Gst.PadProbeReturn.OK

    def silent(self, seconds):
        with self._lock:
            return self.frames >= 48000 * seconds

    def sounding(self, seconds):
        with self._lock:
            return self.audible >= 48000 * seconds

    def close(self):
        self.pipeline.set_state(Gst.State.NULL)


producers = {}


def play(device, frequency):
    pipeline = Gst.parse_launch(
        f"audiotestsrc is-live=true freq={frequency} volume=0.2 ! audioconvert ! audioresample ! "
        f"pulsesink device={device}"
    )
    pipeline.set_state(Gst.State.PLAYING)
    producers[device] = pipeline


def quiet():
    for pipeline in producers.values():
        pipeline.set_state(Gst.State.NULL)
    producers.clear()


def heard(devices, *, sound):
    observers = [Silence(device) for device in devices]
    try:
        wait_until(
            lambda: all(o.sounding(1) if sound else o.silent(1) for o in observers),
            f"private sinks {'sound' if sound else 'are silent'}",
            diagnostics=diagnostics,
        )
    finally:
        for o in observers:
            o.close()


def diagnostics():
    check = app.sound_check
    return dict(
        state=check and check.state,
        busy=check and check.busy,
        outcomes=check and {k: (v.state, v.code) for k, v in (check.outcomes or {}).items()},
        seen=seen,
        captures=len(app.native_captures),
        folders=sorted(str(p.relative_to(folders_root())) for p in folders_root().rglob("*"))
        if folders_root().exists()
        else None,
        error=app.recording_error,
        failure=app.recording_failure,
    )


def no_recording_seen(window, library):
    assert not seen, seen
    assert not app.native_captures and app.recorder is None
    assert application_activity(app) == {}, "never in Recordings running now"
    assert not services.notifications, services.notifications
    assert app.recording_error is None and app.recording_failure is None
    assert not app._recording_cookie
    assert not any(Path(library).iterdir()), "nothing reaches the Library"
    window.monitor.refresh_now()
    wait_until(lambda: not window._pending, "Library rescanned", diagnostics=diagnostics)
    assert not window.snapshot.meetings, "no Library row"


def test(name, window, library):
    check = app.sound_check
    finished = []
    handler = check.connect("finished", lambda c: finished.append(c.state))
    try:
        assert app.can_test_recording(), diagnostics()
        assert app.start_test_recording()
        assert not app.can_test_recording() and check.running
        wait_until(lambda: check.folder is not None, "test folder made", diagnostics=diagnostics)
        folder = check.folder
        assert folder.parent == folders_root()
        assert all(stat.S_IMODE(p.stat().st_mode) == 0o700 for p in (folder, folder.parent, folder.parent.parent))
        wait_until(lambda: check._timer, "both sides decided", diagnostics=diagnostics)
        no_recording_seen(window, library)  # While it runs, too.
        wait_until(lambda: finished, "the 8 s test finishes", timeout=30, diagnostics=diagnostics)
        assert finished == ["result"]
        outcomes = {kind: (o.state, o.code) for kind, o in check.outcomes.items()}
        (recording,) = folder.iterdir()
        files = sorted(p.name for p in recording.iterdir())
        assert stat.S_IMODE(recording.stat().st_mode) == 0o700
        tracks, duration = check.tracks()
        check.close()
        wait_until(lambda: not check.busy and not folder.exists(), "Done deletes the test", diagnostics=diagnostics)
        assert not any(folders_root().iterdir())
        no_recording_seen(window, library)
        evidence["cases"][name] = dict(
            outcomes=outcomes, files=files, tracks=[t.kind for t in tracks], durationMs=duration
        )
        return outcomes, files
    finally:
        check.disconnect(handler)


try:
    app.register(None)
    wait_until(lambda: not leftover.exists(), "a leftover test folder is swept at start", diagnostics=diagnostics)
    assert folders_root().is_dir()
    app.activate()

    def _stop_active():
        if app.recorder and app.recorder.pipeline is not None:
            app.stop_native()
            wait_until(
                lambda: app.recorder.pipeline is None, "failure cleanup finalizes capture", diagnostics=diagnostics
            )

    with (
        tempfile.TemporaryDirectory() as library,
        recording_app_teardown(
            app, on_error=lambda: evidence.setdefault("readiness", diagnostics()), stop_active=_stop_active
        ),
    ):
        os.environ["CASSINI_LIBRARY_DIR"] = library
        window = next(w for w in app.get_windows() if isinstance(w, LibraryWindow))
        window.monitor.refresh_now()
        wait_until(lambda: not window._pending, "Library ready", diagnostics=diagnostics)

        # Nothing plays: audio arrives on both streams, none above the floor.
        heard(("cassini_microphone", "cassini_desktop"), sound=False)
        outcomes, files = test("silent", window, library)
        assert outcomes == dict(microphone=(SILENT, None), system=(SILENT, None)), outcomes
        assert files == ["session.json", "track-microphone.wav", "track-system.wav"], files  # No meeting.json.

        # A tone on the microphone and on this computer's sound.
        play("cassini_microphone", 440)
        play("cassini_desktop", 880)
        heard(("cassini_microphone", "cassini_desktop"), sound=True)
        outcomes, _files = test("tones", window, library)
        assert outcomes == dict(microphone=(WORKING, None), system=(WORKING, None)), outcomes

        # A missing microphone is refused on its own; the computer's sound still works.
        logs = []

        class Refusals(logging.Handler):
            def emit(self, record):
                logs.append(record.getMessage())

        logging.getLogger("cassini_talk.native_recorder").addHandler(Refusals(logging.WARNING))
        parse = Gst.parse_bin_from_description
        with patch.object(
            Gst,
            "parse_bin_from_description",
            side_effect=lambda description, ghost: parse(
                description.replace("device=@DEFAULT_SOURCE@", "device=cassini-device-does-not-exist"), ghost
            ),
        ):
            outcomes, files = test("missing-microphone", window, library)
        assert outcomes == dict(microphone=(REFUSED, "deviceUnavailable"), system=(WORKING, None)), outcomes
        assert files == ["session.json", "track-system.wav"], files
        assert logs == ["Native me side (microphone) did not open at start: Failed to connect stream: No such entity"]

        # A real Record during a test discards it and starts as usual (D60 point 2).
        check = app.sound_check
        shown = []
        check.connect("finished", lambda c: shown.append(c.state))
        app.recording_settings.set_boolean("skip-record-first", True)
        assert app.start_test_recording()
        wait_until(lambda: check._timer, "test recording runs", diagnostics=diagnostics)
        folder = check.folder
        app.on_record()
        assert check.state == "idle" and check.outcomes is None
        wait_until(
            lambda: app.recorder is not None and app.recorder.pipeline is not None and not app.recorder.starting,
            "the real recording starts",
            diagnostics=diagnostics,
        )
        wait_until(lambda: not check.busy and not folder.exists(), "the discarded test is deleted")
        assert not shown, "a discarded test shows no result"
        assert seen[0] == "start_native", seen
        recorder = app.recorder
        assert Path(library) in recorder.session.directory.parents
        assert not app.can_test_recording(), "no test while a recording runs"
        assert not app.start_test_recording()
        wait_until(
            lambda: all(w.frames >= 48000 for w in recorder.session.writers.values()),
            "the real recording captures",
            diagnostics=diagnostics,
        )
        app.on_stop_recording()
        wait_until(lambda: not app.native_captures, "the real recording saves", diagnostics=diagnostics)
        assert app.can_test_recording()
        evidence["recordDiscardsTest"] = True
        quiet()
    print("PASS test recording: silent, tones, missing microphone, swept leftover, Record discards a test")
    assert not callback_errors, callback_errors
    evidence["passed"] = True
except BaseException:
    evidence["failure"] = traceback.format_exc()
    quiet()
    raise
finally:
    services.close()
    evidence.setdefault("readiness", diagnostics())
    evidence["callbackErrors"] = callback_errors
    (artifacts / "results.json").write_text(json.dumps(evidence, indent=2, default=str) + "\n")
