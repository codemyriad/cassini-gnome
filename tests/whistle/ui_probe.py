#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Actual offline GTK -> FIFO -> child -> atomic sidecar -> growing Meeting view."""

import argparse
import hashlib
import json
import os
import sys
import tempfile
import time
import traceback
import wave
from pathlib import Path
from uuid import uuid4

parser = argparse.ArgumentParser()
parser.add_argument("bundle", type=Path)
parser.add_argument("output", type=Path)
parser.add_argument("--resource", type=Path, required=True)
args = parser.parse_args()
assert os.environ.get("CASSINI_WHISTLE_NETWORK_NONE") == "1" and not os.environ.get("WAYLAND_DISPLAY")
assert set(os.listdir("/sys/class/net")) == {"lo"}
root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(root / "src"))
import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gst", "1.0")
from gi.repository import Gio, GLib, Gst, Gtk  # noqa: E402

Gio.resources_register(Gio.Resource.load(str(args.resource)))
from cassini_viewer.folder_player import FolderPlayer  # noqa: E402
from cassini_viewer.library_window import LibraryWindow  # noqa: E402
from cassini_viewer.main import CassiniApplication  # noqa: E402
from cassini_viewer.meeting_store import atomic_json  # noqa: E402
from cassini_viewer.recording_session import RecordingSession  # noqa: E402

errors = []


def exception(kind, value, tb):
    errors.append("".join(traceback.format_exception(kind, value, tb)))
    sys.__excepthook__(kind, value, tb)


sys.excepthook = exception


def until(check, description, timeout=120):
    context = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for _ in range(64):
            if not context.pending():
                break
            context.iteration(False)
        if errors:
            raise AssertionError(errors)
        value = check()
        if value:
            return value
        time.sleep(0.01)
    raise AssertionError("Timed out: " + description)


def walk(widget):
    yield widget
    child = widget.get_first_child()
    while child:
        yield from walk(child)
        child = child.get_next_sibling()


def capture(parent, title, source, tracks):
    session = RecordingSession(parent, title, source=source)
    for identifier, kind, offset in tracks:
        session.add_track(identifier, None, kind, 48000, 1)
        with wave.open(str(root / "tests/whistle/pipeline/pipeline-input-48k.wav")) as audio:
            frames = 0
            while data := audio.readframes(48000):
                session.append(identifier, data, offset + frames * 1000 / 48000)
                frames += len(data) // 2
    session.finish()
    return session


def hashes(directory):
    return {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in directory.iterdir()
        if p.name == "session.json" or p.suffix in (".wav", ".opus")
    }


app = None
report = {}
try:
    with tempfile.TemporaryDirectory(prefix="cassini-pipeline-ui-") as temp:
        temporary = Path(temp)
        library = temporary / "Library"
        library.mkdir()
        schemas = temporary / "schemas"
        schemas.mkdir()
        import shutil
        import subprocess

        shutil.copyfile(root / "data/com.gocassini.Viewer.gschema.xml", schemas / "com.gocassini.Viewer.gschema.xml")
        subprocess.run(["glib-compile-schemas", str(schemas)], check=True)
        os.environ["GSETTINGS_SCHEMA_DIR"] = str(schemas)
        os.environ["CASSINI_LIBRARY_DIR"] = str(library)
        os.environ["CASSINI_WHISTLE_BUNDLE"] = str(args.bundle)
        app = CassiniApplication("com.gocassini.PipelineProof", "test", "development")
        app.register(None)
        app.activate()
        window = next(w for w in app.get_windows() if isinstance(w, LibraryWindow))
        until(lambda: not window._pending, "empty Library loaded")
        app.transcription_enabled = True
        native = capture(
            library, "Native speech", "gnome", [("microphone", "microphone", 500), ("system", "system", 0)]
        )
        native_hash = hashes(native.directory)
        # This is the production native finalisation hook; its audio writer and
        # Pulse sources are separately exercised by the recording integration job.
        app._native_finished(None, str(native.directory))
        browser = capture(
            library,
            "Browser speech",
            "chromium",
            [("local", "local", 1000), ("alice", "remote", 0), ("bob", "remote", 0)],
        )
        browser_hash = hashes(browser.directory)
        window.monitor.refresh()
        partial = until(
            lambda: next(
                (
                    m
                    for m in window.snapshot.meetings
                    if m.directory == native.directory and m.transcript == "running" and m.words
                ),
                None,
            ),
            "partial transcript becomes visible",
        )
        row = next(w for w in walk(window.rows) if getattr(w, "meeting", None) and w.meeting.key == partial.key)
        window.rows.select_row(row)
        window.player.shutdown()
        sink = Gst.ElementFactory.make("fakesink", None)
        sink.set_property("sync", True)
        window.player = FolderPlayer(audio_sink=sink)
        window._connect_player()
        window._loaded_key = None
        window.read_button.emit("clicked")
        assert window.meeting_page and window.meeting_page.meeting.words
        partial_count = len(window.meeting_page.meeting.words)
        until(
            lambda: len(window.meeting_page.meeting.words) > partial_count,
            "Meeting transcript grows during recognition",
        )
        until(
            lambda: all(
                any(m.directory == s.directory and m.transcript == "done" for m in window.snapshot.meetings)
                for s in (native, browser)
            ),
            "both FIFO jobs finish",
        )
        model = window.meeting_page.meeting
        assert len(model.words) == 136 and {s.name for s in model.speakers} == {"Me", "The others"}
        captions = [w.get_label() for w in walk(window.meeting_page.speakers) if isinstance(w, Gtk.Label)]
        assert "Your microphone" in captions and "This computer’s sound" in captions, captions
        assert min(w.start_ms for w in model.words if w.speaker == "me") == 1396
        assert min(w.start_ms for w in model.words if w.speaker == "others") == 896
        other = next(m for m in window.snapshot.meetings if m.directory == browser.directory)
        assert len(other.words) == 204 and other.speaker_names == ("Speaker 1", "Speaker 2", "Me"), other.speaker_names
        assert [s.id for s in other.speakers] == ["track-alice", "track-bob", "me"]
        assert hashes(native.directory) == native_hash and hashes(browser.directory) == browser_hash
        first_child = app.transcriber.client.process
        window.back_to_library()
        # A received plain Opus uses the same worker, on explicit Write it down.
        received = library / f"Received-{uuid4()}"
        received.mkdir()
        original = received / "original.opus"
        encode = Gst.parse_launch(
            "filesrc name=input ! wavparse ! audioconvert ! opusenc ! oggmux ! filesink name=output"
        )
        encode.get_by_name("input").set_property(
            "location", str(root / "tests/whistle/pipeline/pipeline-input-48k.wav")
        )
        encode.get_by_name("output").set_property("location", str(original))
        encode.set_state(Gst.State.PLAYING)
        result = encode.get_bus().timed_pop_filtered(30 * Gst.SECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR)
        assert result and result.type == Gst.MessageType.EOS
        encode.set_state(Gst.State.NULL)
        atomic_json(
            received / "meeting.json",
            dict(
                version=1,
                id=received.name.removeprefix("Received-"),
                revision=0,
                title="Received speech",
                titleSource="user",
                notes=[],
                speakers=[],
                transcript=dict(status="none", reference=None),
                etaMinutes=None,
                error=None,
                quoteOverride=None,
                received=dict(
                    originalName="original.opus",
                    sha256=hashlib.sha256(original.read_bytes()).hexdigest(),
                    importedAtMs=1793107080000,
                ),
            ),
        )
        received_hash = hashes(received)
        window.monitor.refresh()
        received_model = until(
            lambda: next((m for m in window.snapshot.meetings if m.directory == received), None),
            "received meeting published",
        )
        row = next(w for w in walk(window.rows) if getattr(w, "meeting", None) and w.meeting.key == received_model.key)
        window.rows.select_row(row)
        write = next(w for w in walk(window.preview) if isinstance(w, Gtk.Button) and w.get_label() == "Write it down")
        write.emit("clicked")
        received_model = until(
            lambda: next(
                (m for m in window.snapshot.meetings if m.directory == received and m.transcript == "done"), None
            ),
            "received audio written down",
        )
        assert received_model.words and hashes(received) == received_hash
        assert received_model.speaker_names == ("Speaker 1",) and received_model.speakers[0].provenance == "unnamed"
        assert app.transcriber.client.process is first_child, "All jobs must reuse one child"
        # Real worker death -> failed line -> actual Try again control -> new child.
        first_child.kill()
        first_child.wait(5)
        retry = capture(library, "Retry speech", "gnome", [("microphone", "microphone", 0)])
        retry_hash = hashes(retry.directory)
        app.write_down(retry.directory)
        failed = until(
            lambda: next(
                (m for m in window.snapshot.meetings if m.directory == retry.directory and m.transcript == "failed"),
                None,
            ),
            "worker crash becomes failed state",
        )
        row = next(w for w in walk(window.rows) if getattr(w, "meeting", None) and w.meeting.key == failed.key)
        window.rows.select_row(row)
        retry_button = next(
            w for w in walk(window.preview) if isinstance(w, Gtk.Button) and w.get_label() == "Try again"
        )
        retry_button.emit("clicked")
        recovered = until(
            lambda: next(
                (m for m in window.snapshot.meetings if m.directory == retry.directory and m.transcript == "done"), None
            ),
            "Try again succeeds",
        )
        assert len(recovered.words) == 68 and hashes(retry.directory) == retry_hash
        second_child = app.transcriber.client.process
        assert second_child.pid != first_child.pid
        assert "needle" not in sys.modules and "onnxruntime" not in sys.modules, "GTK must not load inference runtimes"
        until(lambda: not app.transcriber.pending, "queue drains")
        report = dict(
            network="none",
            nativeWords=136,
            browserWords=204,
            receivedWords=len(received_model.words),
            receivedUnnamed=True,
            partialTranscriptGrew=True,
            offsetsExact=True,
            sourceCaptions=True,
            remoteTracksSeparate=True,
            captureBytesUnchanged=True,
            workerReused=True,
            crashRetry=True,
            engineAbsentFromGtk=True,
            callbackErrors=errors,
        )
        app.transcriber.close()
        assert second_child.poll() == 0
        app.on_quit()
        until(lambda: not app.get_windows(), "windows close")
except BaseException:
    report["failure"] = traceback.format_exc()
    raise
finally:
    if app and app.transcriber:
        app.transcriber.close()
    args.output.write_text(json.dumps(report, indent=2) + "\n")
print(json.dumps(report, indent=2))
