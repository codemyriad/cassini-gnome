#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Private GTK controls, real D4 transactions, injected native capability only."""

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import threading
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
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402
from test_diarization_models import Probe  # noqa: E402
from test_model_download import remote  # noqa: E402

Gio.resources_register(Gio.Resource.load(str(Path(os.environ["CASSINI_TEST_BUILD"]) / "src/cassini-viewer.gresource")))
from cassini_viewer import nemotron_model  # noqa: E402
from cassini_viewer.diarization_models import DiarizationModels  # noqa: E402
from cassini_viewer.main import CassiniApplication  # noqa: E402
from cassini_viewer.model_download import ModelStore  # noqa: E402
from cassini_viewer.settings_dialog import SettingsDialog  # noqa: E402


def until(check):
    deadline = time.monotonic() + 8
    context = GLib.MainContext.default()
    while time.monotonic() < deadline:
        for _ in range(64):
            if not context.pending():
                break
            context.iteration(False)
        if check():
            return
        time.sleep(0.01)
    assert check(), "Diarization controls did not settle"


fixture = remote.__wrapped__()
server = next(fixture)
try:
    with tempfile.TemporaryDirectory() as temporary:
        tmp = Path(temporary)
        schemas = tmp / "schemas"
        schemas.mkdir()
        shutil.copyfile(ROOT / "data/com.gocassini.Viewer.gschema.xml", schemas / "com.gocassini.Viewer.gschema.xml")
        subprocess.run(["glib-compile-schemas", str(schemas)], check=True)
        os.environ.update(GSETTINGS_SCHEMA_DIR=str(schemas), CASSINI_LIBRARY_DIR=str(tmp / "library"))
        Gtk.Settings.get_default().props.gtk_enable_animations = False
        Adw.StyleManager.get_default().set_color_scheme(
            Adw.ColorScheme.FORCE_DARK
            if os.environ.get("CASSINI_SCREENSHOT_SCHEME") == "dark"
            else Adw.ColorScheme.FORCE_LIGHT
        )
        server.manifest["models"][0].update(
            id=nemotron_model.MODEL_ID, source_sha256=nemotron_model.REVISION, license="OpenMDW-1.1"
        )
        pin = dict(
            size=len(server.data),
            sha256=hashlib.sha256(server.data).hexdigest(),
            downloadBytes=len(server.compressed),
            artifactSHA256=hashlib.sha256(server.compressed).hexdigest(),
        )
        with (
            patch.object(nemotron_model, "FILES", {"weights.onnx": (pin["size"], pin["sha256"], pin["downloadBytes"])}),
            patch.object(nemotron_model, "PIN", dict(license="OpenMDW-1.1", files={"weights.onnx": pin})),
        ):
            app = CassiniApplication("com.gocassini.DiarizationModelsTest", "test", "development")
            app.register(None)
            app.activate()
            window = app.get_active_window()
            until(lambda: not window._pending)
            if app.diarization_models:
                app.diarization_models.close()
            store = ModelStore(tmp / "models", origin=server.origin, fixture=True)
            app.diarization_models = DiarizationModels(
                tmp / "bundle",
                lambda: GLib.idle_add(app._models_changed),
                store=store,
                client_factory=Probe,
            )
            until(lambda: not app.diarization_models.snapshot()["busy"])
            dialog = app.settings_dialog = SettingsDialog(app)
            dialog.present(window)
            dialog.set_visible_page_name("transcription")
            assert dialog.nemotron_action.get_label() == "Download" and not server.requests
            assert dialog.nemotron_status.get_label() == "Not downloaded"
            # Hold before the real transaction to make Cancel observable without timing races.
            entered, release = threading.Event(), threading.Event()
            original = store.download

            def held(*args, **kwargs):
                entered.set()
                assert release.wait(5)
                return original(*args, **kwargs)

            with patch.object(store, "download", held):
                dialog.nemotron_action.emit("clicked")
                until(entered.is_set)
                assert dialog.nemotron_action.get_label() == "Cancel" and dialog.nemotron_progress.get_visible()
                dialog.nemotron_action.emit("clicked")
                release.set()
                until(
                    lambda: (
                        not app.diarization_models.snapshot()["busy"]
                        and dialog.nemotron_action.get_label() == "Download"
                    )
                )
                assert not app.diarization_models.snapshot()["downloaded"]
                assert dialog.nemotron_action.get_label() == "Download"
            server.mode = "truncate"
            dialog.nemotron_action.emit("clicked")
            until(
                lambda: (
                    not app.diarization_models.snapshot()["busy"] and dialog.nemotron_action.get_label() == "Try again"
                )
            )
            assert dialog.nemotron_action.get_label() == "Try again"
            assert "Nemotron" in dialog.nemotron_line.get_label()
            server.mode = "normal"
            dialog.nemotron_action.emit("clicked")
            until(
                lambda: (
                    not app.diarization_models.snapshot()["busy"]
                    and dialog.nemotron_status.get_label() == "On"
                    and dialog.nemotron_action.get_label() == "Remove"
                )
            )
            assert dialog.nemotron_status.get_label() == "On" and dialog.nemotron_action.get_label() == "Remove"
            assert app.recording_settings.get_string("speech-model") == "whistle"
            assert (tmp / "models" / nemotron_model.MODEL_ID / nemotron_model.REVISION).is_dir()
            app.diarization_models._change(ready=False, error=True, runtime_error=True)
            until(lambda: dialog.nemotron_action.get_label() == "Try again")
            assert dialog.nemotron_remove.get_visible() and not dialog.nemotron_line.get_visible()
            dialog.nemotron_action.emit("clicked")
            until(
                lambda: (
                    not app.diarization_models.snapshot()["busy"]
                    and dialog.nemotron_status.get_label() == "On"
                    and not dialog.nemotron_remove.get_visible()
                )
            )
            assert dialog.nemotron_status.get_label() == "On" and not dialog.nemotron_remove.get_visible()
            dialog.nemotron_action.emit("clicked")
            until(
                lambda: (
                    not app.diarization_models.snapshot()["busy"]
                    and dialog.nemotron_status.get_label() == "Not downloaded"
                    and dialog.nemotron_action.get_label() == "Download"
                )
            )
            assert dialog.nemotron_status.get_label() == "Not downloaded"
            assert dialog.nemotron_action.get_label() == "Download"
            assert not (tmp / "models" / nemotron_model.MODEL_ID / nemotron_model.REVISION).exists()
            dialog.force_close()
            app.begin_exit()
            window.close()
            app.do_shutdown()
            assert "onnxruntime" not in sys.modules and "cassini_viewer.nemotron" not in sys.modules
            print("PASS Nemotron GTK: Download/Cancel/failure/Try again/On/Remove; no engine in GTK")
finally:
    fixture.close()
