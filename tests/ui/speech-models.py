#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Native model controls drive the real D4 store against tiny loopback weights."""

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import time
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
from test_model_download import remote  # noqa: E402

Gio.resources_register(Gio.Resource.load(str(Path(os.environ["CASSINI_TEST_BUILD"]) / "src/cassini-viewer.gresource")))
from cassini_viewer import parakeet_model  # noqa: E402
from cassini_viewer.main import CassiniApplication  # noqa: E402
from cassini_viewer.model_download import ModelStore  # noqa: E402
from cassini_viewer.parakeet_model import MODEL_ID, REVISION  # noqa: E402
from cassini_viewer.settings_dialog import SettingsDialog  # noqa: E402
from cassini_viewer.speech_models import SpeechModels  # noqa: E402


def until(check):
    end = time.monotonic() + 8
    context = GLib.MainContext.default()
    while time.monotonic() < end:
        for _ in range(64):
            if not context.pending():
                break
            context.iteration(False)
        if check():
            return
        time.sleep(0.01)
    assert check(), "Model GUI did not settle"


def capture(window, suffix):
    target = Path(os.environ["CASSINI_SCREENSHOT_OUT"])
    target.parent.mkdir(parents=True, exist_ok=True)
    paintable = Gtk.WidgetPaintable.new(window)
    ticks = []
    window.add_tick_callback(lambda *_: (ticks.append(True), len(ticks) < 3)[1])
    window.queue_draw()
    until(lambda: len(ticks) >= 3)
    snapshot = Gtk.Snapshot.new()
    paintable.snapshot(snapshot, window.get_width(), window.get_height())
    texture = window.get_renderer().render_texture(snapshot.to_node(), None)
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
                store=ModelStore(tmp / "models", origin=server.origin, fixture=True),
            )
            until(lambda: not app.speech_models.snapshot()["busy"])
            dialog = app.settings_dialog = SettingsDialog(app)
            dialog.present(window)
            dialog.set_visible_page_name("transcription")
            assert not dialog.parakeet_radio.get_sensitive() and dialog.whistle_radio.get_active()
            assert dialog.parakeet_status.get_label() == "443 MB download"
            assert dialog.parakeet_action.get_label() == "Download" and not server.requests
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
                    and dialog.parakeet_status.get_label() == "Downloaded"
                )
            )
            assert dialog.parakeet_radio.get_sensitive() and dialog.whistle_radio.get_active()
            assert dialog.parakeet_status.get_label() == "Downloaded"
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
            app.begin_exit()
            window.close()
            app.do_shutdown()
            assert "onnxruntime" not in sys.modules and "cassini_viewer.parakeet" not in sys.modules
            print("PASS speech models: Download/retry/selection/Remove, persisted fallback, no engine in GTK")
finally:
    fixture.close()
