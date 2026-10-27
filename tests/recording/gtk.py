#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Real GTK recording/session controls and quit finalization inside the test display."""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

if not os.environ.get("CASSINI_ISOLATED_AUDIO") or os.environ.get("WAYLAND_DISPLAY"):
    raise SystemExit("Use tests/recording/run.sh")
parser = argparse.ArgumentParser()
parser.add_argument("--build", required=True)
args = parser.parse_args()
build = Path(args.build).resolve()
schema_dir = build / "recording-test-schemas"
schema_dir.mkdir(exist_ok=True)
shutil.copyfile(
    Path(__file__).resolve().parents[2] / "data/com.gocassini.Viewer.gschema.xml",
    schema_dir / "com.gocassini.Viewer.gschema.xml",
)
subprocess.run(["glib-compile-schemas", str(schema_dir)], check=True)
os.environ["GSETTINGS_SCHEMA_DIR"] = str(schema_dir)
import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gio, Gst  # noqa: E402
from readiness import PCMProgress, wait_until  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
Gio.resources_register(Gio.Resource.load(str(build / "src/cassini-viewer.gresource")))
from cassini_viewer.library_window import LibraryWindow  # noqa: E402
from cassini_viewer.main import CassiniApplication  # noqa: E402
from cassini_viewer.recording_session import RecordingSession  # noqa: E402


def painted(window):
    frames = []

    def tick(widget, _clock):
        if widget.get_mapped() and widget.get_width() > 0 and widget.get_height() > 0:
            frames.append(True)
        return len(frames) < 2

    callback = window.add_tick_callback(tick)
    try:
        wait_until(lambda: len(frames) >= 2, "window allocated and painted", diagnostics=diagnostics)
    finally:
        if len(frames) < 2:
            window.remove_tick_callback(callback)


artifacts = Path(os.environ.get("CASSINI_TEST_ARTIFACTS", str(Path(__file__).parent / "artifacts/gtk")))
artifacts.mkdir(parents=True, exist_ok=True)
app = CassiniApplication("com.gocassini.RecordingTest", "test", "development")
app.transcription_enabled = False  # Audio/permission harness; real ML is proved by the offline pipeline job.
evidence = {"passed": False}
callback_errors = []


def callback_error(kind, value, tb):
    callback_errors.append("".join(traceback.format_exception(kind, value, tb)))
    sys.__excepthook__(kind, value, tb)


sys.excepthook = callback_error


def diagnostics():
    return {
        "windows": [
            dict(type=type(w).__name__, mapped=w.get_mapped(), width=w.get_width(), height=w.get_height())
            for w in app.get_windows()
        ]
    }


try:
    app.register(None)
    app.activate()
    producers, observations, monitors = [], [], []
    for device, frequency in (("cassini_microphone", 440), ("cassini_desktop", 880)):
        pipeline = Gst.parse_launch(
            f"audiotestsrc is-live=true freq={frequency} volume=0.2 ! audioconvert ! audioresample ! pulsesink device={device}"
        )
        pipeline.set_state(Gst.State.PLAYING)
        producers.append(pipeline)
        monitor = Gst.parse_launch(
            f"pulsesrc device={device}.monitor ! audioconvert ! audioresample ! audio/x-raw,format=S16LE,rate=48000,channels=1 ! fakesink name=observed sync=false"
        )
        observations.append(PCMProgress(monitor.get_by_name("observed").get_static_pad("sink"), observe_audio=True))
        monitors.append(monitor)
        monitor.set_state(Gst.State.PLAYING)
    wait_until(
        lambda: all(o.reached(1, audible=True) for o in observations),
        "private microphone and desktop deliver audible PCM",
        diagnostics=diagnostics,
    )
    for o in observations:
        o.close()
    for m in monitors:
        m.set_state(Gst.State.NULL)
    with tempfile.TemporaryDirectory() as directory:
        os.environ["CASSINI_LIBRARY_DIR"] = directory
        # The first activation used an empty private default; reopen against this Library.
        for w in list(app.get_windows()):
            w.close()
        app.activate()
        window = next(w for w in app.get_windows() if isinstance(w, LibraryWindow))
        wait_until(lambda: not window._pending, "Library ready", diagnostics=diagnostics)
        app.on_record()
        dialog = app.record_first
        assert dialog is not None and app.recorder is None
        dialog.dont_show.set_active(True)
        dialog.start_button.emit("clicked")
        wait_until(
            lambda: app.recorder and app.recorder.pipeline is not None,
            "native recording starts",
            diagnostics=diagnostics,
        )
        recorder = app.recorder

        def captured():
            assert not app.recording_error, app.recording_error
            return all(w.frames >= 48000 * 0.4 for w in recorder.session.writers.values())

        wait_until(captured, "both native tracks capture actual PCM", diagnostics=diagnostics)
        wait_until(
            lambda: all(level > 0.1 for level in recorder.levels),
            "both real level meters respond",
            diagnostics=diagnostics,
        )
        wait_until(
            lambda: window.snapshot.meetings and window.snapshot.meetings[0].lifecycle == "recording",
            "native live Library row",
            diagnostics=diagnostics,
        )
        assert window.live_stop.get_sensitive() and window.capture_header.get_visible_child_name() == "live"
        assert all(bar.get_fraction() > 0.1 for bar in window.live_levels)
        painted(window)
        subprocess.run(["import", "-window", "root", str(artifacts / "recording.png")], check=True)
        before = min(w.frames for w in recorder.session.writers.values())
        app.on_quit()
        assert app._capture_confirmation
        app._capture_confirmation.emit("response", "keep")
        wait_until(
            lambda: all(w.frames >= before + 48000 * 0.4 for w in recorder.session.writers.values()),
            "Keep recording actually keeps writing",
            diagnostics=diagnostics,
        )
        app.activate()
        window.live_stop.emit("clicked")
        wait_until(lambda: recorder.pipeline is None, "live Stop finalizes native session", diagnostics=diagnostics)
        wait_until(
            lambda: window.snapshot.meetings[0].lifecycle == "saved",
            "native saved Library row",
            diagnostics=diagnostics,
        )
        assert window.snapshot.meetings[0].interrupted_at_ms is None
        saved = recorder.session.directory
        manifest = json.loads((saved / "session.json").read_text())
        assert manifest["source"] == "gnome" and manifest["state"] == "completed"
        assert {t["file"] for t in manifest["tracks"]} == {"track-microphone.wav", "track-system.wav"}
        import math
        import struct
        import wave

        tones = []
        for track, frequency, other in zip(manifest["tracks"], (440, 880), (880, 440), strict=True):
            with wave.open(str(saved / track["file"])) as audio:
                assert audio.getnchannels() == 1 and audio.getframerate() == 48000
                assert audio.getnframes() >= 48000 * 0.4
                values = [
                    v / 32768
                    for v in struct.unpack("<" + "h" * audio.getnframes(), audio.readframes(audio.getnframes()))
                ]

            def magnitude(hz, values=values):
                block = 4800
                chunks = [values[i : i + block] for i in range(4800, len(values) - block, block)]
                return sum(
                    2
                    * abs(
                        sum(
                            v * complex(math.cos(2 * math.pi * hz * j / 48000), math.sin(2 * math.pi * hz * j / 48000))
                            for j, v in enumerate(chunk)
                        )
                    )
                    / len(chunk)
                    for chunk in chunks
                ) / len(chunks)

            desired, bleed = magnitude(frequency), magnitude(other)
            assert desired > 0.1 and bleed < desired / 5, (desired, bleed)
            tones.append(dict(frequency=frequency, magnitude=desired, bleed=bleed))
        evidence["nativeTracks"] = manifest
        evidence["tones"] = tones
        # The persisted preference starts a second recording without another dialog.
        app.on_record()
        wait_until(
            lambda: recorder.pipeline is not None, "skip-first preference starts recording", diagnostics=diagnostics
        )
        assert app.record_first is None
        wait_until(captured, "quit test captures actual PCM", diagnostics=diagnostics)
        app.on_quit()
        assert app._capture_confirmation
        app._capture_confirmation.emit("response", "stop")
        wait_until(
            lambda: recorder.pipeline is None and not app.get_windows(),
            "quit flushes and closes windows",
            timeout=12,
            diagnostics=diagnostics,
        )
        assert recorder.session.manifest["state"] == "completed"
        session = RecordingSession(directory, "Browser test meeting")
        session.add_track("a", "Remote source", "remote", 48000, 1)
        session.append("a", bytes(96000), 250)
        session.finish()
        app.open_in_new_window(Gio.File.new_for_path(str(session.directory / "session.json")))
        wait_until(
            lambda: any(isinstance(w, LibraryWindow) and w.meeting_page for w in app.get_windows()),
            "recording folder opens the pushed Meeting page",
            diagnostics=diagnostics,
        )
        viewer = next(w for w in app.get_windows() if isinstance(w, LibraryWindow))
        painted(viewer)
        assert viewer.meeting_page.play.get_sensitive()
        assert viewer.meeting_page.player.duration == 1250  # 250 ms source offset + 1 second PCM
        viewer.meeting_page.play.emit("clicked")
        wait_until(
            lambda: viewer.meeting_page.player.position > 0,
            "session playback advances",
            timeout=3,
            diagnostics=diagnostics,
        )
        assert viewer.meeting_page.player.position > 0
        assert viewer.meeting_page.play.get_tooltip_text() == "Pause"
        viewer.meeting_page.play.emit("clicked")
        assert not viewer.meeting_page.player.playing
        subprocess.run(["import", "-window", "root", str(artifacts / "session.png")], check=True)
        viewer.close()
    for producer in producers:
        producer.set_state(Gst.State.NULL)
    print("PASS native Library RecordFirst, separate Pulse WAVs/levels/Stop, keep-background, quit and pushed playback")
    assert not callback_errors, callback_errors
    evidence["passed"] = True
except BaseException:
    evidence["failure"] = traceback.format_exc()
    for producer in locals().get("producers", []):
        producer.set_state(Gst.State.NULL)
    subprocess.run(["import", "-window", "root", str(artifacts / "failure.png")], check=False)
    raise
finally:
    evidence["readiness"] = diagnostics()
    evidence["callbackErrors"] = callback_errors
    (artifacts / "results.json").write_text(json.dumps(evidence, indent=2) + "\n")
