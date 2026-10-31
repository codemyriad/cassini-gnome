#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Real GTK Quit with a driverless PipeWire mic and healthy system PCM."""

import argparse
import faulthandler
import json
import os
import shutil
import subprocess
import sys
import time
import traceback
import wave
from pathlib import Path

if os.environ.get("CASSINI_ISOLATED_AUDIO") != "1" or os.environ.get("WAYLAND_DISPLAY"):
    raise SystemExit("Use tests/recording/run.sh")
parser = argparse.ArgumentParser()
parser.add_argument("--build", required=True, type=Path)
args = parser.parse_args()
root = Path(__file__).resolve().parents[2]
output = Path(os.environ["CASSINI_TEST_ARTIFACTS"]) / "no-frames"
output.mkdir(parents=True)
schemas = output / "schemas"
schemas.mkdir()
for schema in (root / "data").glob("*.gschema.xml"):
    shutil.copyfile(schema, schemas / schema.name)
subprocess.run(["glib-compile-schemas", "--strict", str(schemas)], check=True)
os.environ.update(
    GSETTINGS_SCHEMA_DIR=str(schemas), GSETTINGS_BACKEND="memory", CASSINI_LIBRARY_DIR=str(output / "Library")
)
sys.path[:0] = [str(root / "src"), str(root / "tests")]
import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gst", "1.0")
from desktop_services import DesktopServices  # noqa: E402
from gi.repository import Gio, Gst  # noqa: E402
from private_pipewire import PrivatePipeWire  # noqa: E402
from readiness import wait_until  # noqa: E402

Gio.resources_register(Gio.Resource.load(str(args.build.resolve() / "src/cassini-viewer.gresource")))
from cassini_viewer.main import CassiniApplication  # noqa: E402

Gst.init(None)
services = DesktopServices()
app = None
producer = None
report = dict(passed=False)
errors = []
fault = (output / "threads.log").open("w")
# Bound even C-level state changes/interpreter shutdown and retain every trace.
faulthandler.dump_traceback_later(35, file=fault, exit=True)


def callback_error(kind, value, tb):
    errors.append("".join(traceback.format_exception(kind, value, tb)))
    sys.__excepthook__(kind, value, tb)


sys.excepthook = callback_error


def diagnostics():
    native = app.recorder if app else None
    return dict(
        callbackErrors=errors,
        quitting=app._quitting if app else None,
        windows=len(app.get_windows()) if app else 0,
        problem=native._problem if native else None,
        finishing=native._finishing if native else None,
        pipeline=bool(native and native.pipeline),
        frames={kind: writer.frames for kind, writer in native.session.writers.items()}
        if native and native.session
        else {},
    )


server = PrivatePipeWire(output)
try:
    app = CassiniApplication("com.gocassini.NoFrameQuitTest", "test", "development")
    app.transcription_enabled = False
    app.register(None)
    app.activate()
    wait_until(
        lambda: app.get_active_window() and app.get_active_window().get_mapped(),
        "Library window",
        diagnostics=diagnostics,
    )
    producer = Gst.parse_launch(
        "audiotestsrc is-live=true freq=880 volume=0.2 ! audioconvert ! pulsesink device=speakers"
    )
    producer.set_state(Gst.State.PLAYING)
    app.start_native()
    wait_until(
        lambda: app.recorder and app.recorder.session and app.recorder.session.writers["system"].frames >= 24000,
        "healthy system PCM alongside a no-frame microphone",
        diagnostics=diagnostics,
    )
    recorder = app.recorder
    session = recorder.session
    assert session.writers["microphone"].frames == 0
    path = session.directory / "track-system.wav"
    before = path.read_bytes()[44:]
    frames = session.writers["system"].frames
    assert before and any(before)
    app.activate_action("quit", None)
    wait_until(lambda: app._capture_confirmation, "production Quit confirmation", diagnostics=diagnostics)
    dialog = app._capture_confirmation
    started = time.monotonic()
    dialog.emit("response", "stop")
    dialog.force_close()
    wait_until(
        lambda: recorder.pipeline is None and not recorder._finishing and not app.get_windows() and app._quitting,
        "Quit saves healthy audio and closes with a no-frame mic",
        timeout=3,
        diagnostics=diagnostics,
    )
    elapsed = time.monotonic() - started
    assert not recorder._problem and not app.recording_error and not errors, diagnostics()
    assert session.manifest.get("problemKind") in (None, "gap"), session.manifest
    assert all(track["ended"] for track in session.manifest["tracks"])
    assert session.writers["microphone"].frames == 0
    assert path.read_bytes()[44:].startswith(before), "Quit lost already captured PCM"
    with wave.open(str(path), "rb") as audio:
        assert audio.getnframes() >= frames
        assert audio.readframes(len(before) // 2) == before
    ends = [event for event in session.manifest["events"] if event["type"] == "end"]
    assert len(ends) == 1 and ends[0]["reason"] == "appQuit", session.manifest
    report.update(
        passed=True,
        quitSeconds=elapsed,
        microphoneFrames=0,
        keptSystemFrames=frames,
        finalSystemFrames=session.writers["system"].frames,
        state=session.manifest["state"],
    )
    print("PASS Quit with no microphone frames: healthy PCM kept, windows closed", report, flush=True)
except BaseException:
    report["failure"] = traceback.format_exc()
    faulthandler.dump_traceback(file=fault)
    raise
finally:
    report["diagnostics"] = diagnostics()
    (output / "result.json").write_text(json.dumps(report, indent=2) + "\n")
    if producer is not None:
        producer.set_state(Gst.State.NULL)
    server.close()
# Keep the faulthandler deadline armed through interpreter shutdown too:
# a live non-daemon recorder worker must fail this process, not hang CI.
