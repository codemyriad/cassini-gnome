#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Native model controls drive the real D4 store against tiny loopback weights."""

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

if os.environ.get("CASSINI_SCREENSHOT_ISOLATED") != "1" or os.environ.get("WAYLAND_DISPLAY"):
    raise SystemExit("Use build-aux/ux-screenshot.sh")
ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]
import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gst", "1.0")
gi.require_version("Gsk", "4.0")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402
from painted import painted_node  # noqa: E402
from test_model_download import remote  # noqa: E402

Gio.resources_register(Gio.Resource.load(str(Path(os.environ["CASSINI_TEST_BUILD"]) / "src/cassini-talk.gresource")))
from cassini_talk import parakeet_model  # noqa: E402
from cassini_talk.main import CassiniApplication  # noqa: E402
from cassini_talk.model_download import ModelStore  # noqa: E402
from cassini_talk.parakeet_model import MODEL_ID, REVISION  # noqa: E402
from cassini_talk.settings_dialog import SettingsDialog  # noqa: E402
from cassini_talk.speech_models import SpeechModels  # noqa: E402


def until(check, message="Model GUI did not settle"):
    context = GLib.MainContext.default()
    expired = False

    def expire(*_args):
        nonlocal expired
        expired = True
        return GLib.SOURCE_REMOVE

    guard = GLib.timeout_source_new_seconds(18)
    guard.set_callback(expire)
    guard.attach(context)
    try:
        while not check() and not expired:
            context.iteration(True)
        assert check(), message
    finally:
        guard.destroy()


def capture(window, suffix):
    target = Path(os.environ["CASSINI_SCREENSHOT_OUT"])
    target.parent.mkdir(parents=True, exist_ok=True)
    ticks = []
    window.add_tick_callback(lambda *_: (ticks.append(True), len(ticks) < 3)[1])
    window.queue_draw()
    until(lambda: len(ticks) >= 3)
    texture = window.get_renderer().render_texture(painted_node(window), None)
    assert texture.save_to_png(str(target.with_name(target.stem + suffix + ".png")))


fixture = remote.__wrapped__()
server = next(fixture)
try:
    with tempfile.TemporaryDirectory() as temporary:
        tmp = Path(temporary)
        schemas = tmp / "schemas"
        schemas.mkdir()
        shutil.copyfile(
            ROOT / "data/io.codemyriad.CassiniTalk.gschema.xml", schemas / "io.codemyriad.CassiniTalk.gschema.xml"
        )
        subprocess.run(["glib-compile-schemas", str(schemas)], check=True)
        os.environ.update(GSETTINGS_SCHEMA_DIR=str(schemas), CASSINI_LIBRARY_DIR=str(tmp / "library"))
        Gtk.Settings.get_default().props.gtk_enable_animations = False
        Adw.StyleManager.get_default().set_color_scheme(
            Adw.ColorScheme.FORCE_DARK
            if os.environ.get("CASSINI_SCREENSHOT_SCHEME") == "dark"
            else Adw.ColorScheme.FORCE_LIGHT
        )
        server.manifest["models"][0].update(id=MODEL_ID, source_sha256=REVISION)
        with patch.object(
            parakeet_model,
            "FILES",
            {"weights.onnx": (len(server.data), hashlib.sha256(server.data).hexdigest(), len(server.compressed))},
        ):
            app = CassiniApplication("com.gocassini.SpeechModelsTest", "test", "development")
            app.register(None)
            app.activate()
            window = app.get_active_window()
            window.set_default_size(1130, 730)
            until(lambda: not window._pending)
            app.speech_models = SpeechModels(
                "whistle",
                lambda value: app.recording_settings.set_string("speech-model", value),
                lambda: GLib.idle_add(app._models_changed),
                capable=lambda: True,
                preference=app.recording_settings.get_string("speech-model-preference"),
                persist_preference=lambda value: app.recording_settings.set_string("speech-model-preference", value),
                store=ModelStore(
                    tmp / "models",
                    origin=server.origin,
                    fixture=True,
                    user_agent="CassiniTalk/test (GNOME; Linux; source)",
                ),
            )
            until(lambda: not app.speech_models.snapshot()["busy"])
            notices = []
            add_toast = window.toasts.add_toast

            def toast_shown(toast):
                notices.append(toast.get_title())
                add_toast(toast)

            window.toasts.add_toast = toast_shown
            dialog = app.settings_dialog = SettingsDialog(app)
            dialog.present(window)
            dialog.set_visible_page_name("transcription")
            assert not dialog.parakeet_radio.get_sensitive() and dialog.whistle_radio.get_active()
            assert dialog.parakeet_status.get_label() == "444 MB download"
            assert dialog.parakeet_action.get_label() == "Download" and not server.requests
            until(lambda: dialog.parakeet_suggestion.get_visible())
            app.speech_models.capable = lambda: False
            app.speech_models.refresh_suggestion()
            until(
                lambda: not dialog.parakeet_suggestion.get_visible(),
                "Settings did not remove the ineligible Parakeet suggestion",
            )
            app.speech_models.capable = lambda: True
            app.speech_models.refresh_suggestion()
            until(lambda: dialog.parakeet_suggestion.get_visible())
            capture(window, "-suggestion")
            assert not window.parakeet_suggestion.get_visible()
            assert not app.recording_settings.get_boolean("whistle-transcribed")
            # Complete a real FIFO job against a tiny injected recognizer. The
            # production commit event, not a fabricated Library snapshot, drives D78.
            from durable import Finished
            from test_library import capture as make_capture

            from cassini_talk.transcription import TranscriptionQueue

            class Starter:
                def __init__(self, *_args, **_kwargs):
                    pass

                def start(self):
                    pass

                def close(self):
                    pass

                def process_tracks(self, tracks, notify):
                    notify(dict(type="planned", etaMinutes=0))
                    notify(
                        dict(
                            type="progress",
                            etaMinutes=0,
                            trackId=tracks[0]["id"],
                            trackOrder=0,
                            language="en",
                            words=[dict(startMs=0, endMs=10, text="Hello")],
                        )
                    )
                    return dict(diagnostics=[])

            own = tmp / "first-whistle"
            session = make_capture(own)
            finished = Finished()
            queue = TranscriptionQueue(
                lambda _: None,
                finished,
                root=own,
                bundle=tmp,
                client_factory=Starter,
                completed=lambda model: GLib.idle_add(app._model_completed, model),
            )
            try:
                assert queue.enqueue(session.directory)
                finished.wait(1, exactly=True)
                until(lambda: window.parakeet_suggestion.get_visible())
                assert app.recording_settings.get_boolean("whistle-transcribed")
                assert app.recording_settings.get_boolean("parakeet-suggestion-shown")
                window.parakeet_suggestion.dismiss.emit("clicked")
                until(lambda: not dialog.parakeet_suggestion.get_visible())
                assert not window.parakeet_suggestion.get_visible()
                assert app.recording_settings.get_boolean("parakeet-suggestion-dismissed")
            finally:
                queue.close()
            capture(window, "-available")
            # A failed request stays retryable and never selects the model.
            server.mode = "truncate"
            dialog.parakeet_action.emit("clicked")
            until(
                lambda: not app.speech_models.snapshot()["busy"] and dialog.parakeet_action.get_label() == "Try again"
            )
            assert dialog.parakeet_action.get_label() == "Try again"
            assert "Could not download Parakeet Ultra" in dialog.model_line.get_label()
            server.mode = "normal"
            dialog.parakeet_action.emit("clicked")
            until(
                lambda: (
                    not app.speech_models.snapshot()["busy"]
                    and dialog.parakeet_radio.get_sensitive()
                    and dialog.parakeet_status.get_label() == "In use for new meetings"
                )
            )
            assert dialog.parakeet_radio.get_sensitive() and dialog.parakeet_radio.get_active()
            assert app.recording_settings.get_boolean("parakeet-default-announced")
            assert notices.count("New meetings now use Parakeet Ultra.") == 1
            app._models_changed()
            assert notices.count("New meetings now use Parakeet Ultra.") == 1
            dialog.whistle_radio.set_active(True)
            assert app.recording_settings.get_string("speech-model-preference") == "whistle"
            dialog.parakeet_radio.set_active(True)
            assert app.recording_settings.get_string("speech-model") == MODEL_ID
            assert dialog.parakeet_status.get_label() == "In use for new meetings"
            assert not dialog.whistle_status.get_visible()
            capture(window, "-selected")
            dialog.parakeet_action.emit("clicked")
            assert app.recording_settings.get_string("speech-model") == "whistle"
            until(
                lambda: (
                    not app.speech_models.snapshot()["busy"]
                    and not dialog.parakeet_radio.get_sensitive()
                    and dialog.parakeet_action.get_label() == "Download"
                )
            )
            assert dialog.whistle_radio.get_active() and not dialog.parakeet_radio.get_sensitive()
            assert dialog.model_line.get_label() == "Parakeet Ultra was removed. New meetings use Whistle."
            assert not (tmp / "models" / MODEL_ID / REVISION).exists()
            capture(window, "-removed")
            dialog.force_close()
            until(lambda: dialog.closed)
            dialog = app.settings_dialog = SettingsDialog(app)
            dialog.present(window)
            assert not dialog.parakeet_suggestion.get_visible()
            assert notices.count("New meetings now use Parakeet Ultra.") == 1
            dialog.force_close()
            app.begin_exit()
            window.close()
            app.do_shutdown()
            assert "onnxruntime" not in sys.modules and "cassini_talk.parakeet" not in sys.modules
            print("PASS speech models: Download/retry/selection/Remove, persisted fallback, no engine in GTK")
finally:
    fixture.close()
