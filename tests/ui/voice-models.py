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
from dispatch_wait import dispatch_until  # noqa: E402
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402
from test_model_download import remote  # noqa: E402
from test_voice_models import Probe  # noqa: E402

Gio.resources_register(Gio.Resource.load(str(Path(os.environ["CASSINI_TEST_BUILD"]) / "src/cassini-talk.gresource")))
from cassini_talk import campplus_model  # noqa: E402
from cassini_talk.main import CassiniApplication  # noqa: E402
from cassini_talk.model_download import ModelStore  # noqa: E402
from cassini_talk.settings_dialog import SettingsDialog  # noqa: E402
from cassini_talk.voice_models import VoiceModels  # noqa: E402


def retry_with_delayed_ready_notification(app, dialog):
    models = app.voice_models
    entered, release = threading.Event(), threading.Event()
    check_runtime = models._check_runtime

    def held_runtime(directory):
        GLib.idle_add(entered.set)
        assert release.wait(18), "the test did not release the voice runtime probe"
        return check_runtime(directory)

    with patch.object(models, "_check_runtime", held_runtime):
        try:
            dialog.voice_action.emit("clicked")
            dispatch_until(entered.is_set, "the voice retry did not enter its runtime probe")
            assert models.snapshot()["busy"] and dialog.voice_status.get_label() == "Downloaded"
            assert dialog.voice_action.get_label() == "Remove"
            release.set()
            # Finish the real worker while GTK still displays the intermediate
            # state. Its completion notifications remain queued on this context.
            models._thread.join(18)
            assert not models._thread.is_alive(), "the voice runtime retry did not finish"
            assert not models.snapshot()["busy"] and dialog.voice_status.get_label() == "Downloaded"
            dispatch_until(
                lambda: (
                    not models.snapshot()["busy"]
                    and dialog.voice_status.get_label() == "On"
                    and dialog.voice_action.get_label() == "Remove"
                    and not dialog.voice_remove.get_visible()
                ),
                "the voice runtime retry did not reach its rendered ready state",
            )
            assert dialog.voice_status.get_label() == "On" and not dialog.voice_remove.get_visible(), (
                "voice retry accepted the intermediate Downloaded state"
            )
        finally:
            release.set()


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
        server.manifest["models"][0].update(
            id=campplus_model.MODEL_ID, source_sha256=campplus_model.REVISION, license="Apache-2.0"
        )
        pin = dict(
            size=len(server.data),
            sha256=hashlib.sha256(server.data).hexdigest(),
            downloadBytes=len(server.compressed),
            artifactSHA256=hashlib.sha256(server.compressed).hexdigest(),
        )
        with (
            patch.object(campplus_model, "FILES", {"weights.onnx": (pin["size"], pin["sha256"], pin["downloadBytes"])}),
            patch.object(campplus_model, "PIN", dict(license="Apache-2.0", files={"weights.onnx": pin})),
        ):
            app = CassiniApplication("com.gocassini.VoiceModelsTest", "test", "development")
            app.register(None)
            app.activate()
            window = app.get_active_window()
            dispatch_until(lambda: not window._pending)
            if app.voice_models:
                app.voice_models.close()
            store = ModelStore(
                tmp / "models", origin=server.origin, fixture=True, user_agent="CassiniTalk/test (GNOME; Linux; source)"
            )
            app.voice_models = VoiceModels(
                tmp / "bundle",
                lambda: GLib.idle_add(app._models_changed),
                store=store,
                client_factory=Probe,
            )
            dispatch_until(lambda: not app.voice_models.snapshot()["busy"])
            from types import SimpleNamespace

            dependency = dict(ready=False, downloaded=False, busy=False, downloading=False, percent=0, error=False)
            app.diarization_models = SimpleNamespace(snapshot=lambda: dict(dependency), close=lambda: None)
            dialog = app.settings_dialog = SettingsDialog(app)
            dialog.present(window)
            dialog.set_visible_page_name("transcription")
            assert not dialog.voice_action.get_sensitive()
            assert dialog.voice_status.get_label() == "Needs “Tell speakers apart” first"
            dependency.update(ready=True, downloaded=True)
            dialog.render_diarization()
            assert dialog.voice_action.get_sensitive()
            assert dialog.voice_action.get_label() == "Download" and not server.requests
            assert dialog.voice_status.get_label() == "Not downloaded"
            # Hold before the real transaction to make Cancel observable without timing races.
            entered, release = threading.Event(), threading.Event()
            original = store.download

            def held(*args, **kwargs):
                GLib.idle_add(entered.set)
                assert release.wait(18), "the test did not release the voice download"
                return original(*args, **kwargs)

            with patch.object(store, "download", held):
                dialog.voice_action.emit("clicked")
                try:
                    dispatch_until(entered.is_set, "the voice download did not enter")
                    assert dialog.voice_action.get_label() == "Cancel" and dialog.voice_progress.get_visible()
                    dialog.voice_action.emit("clicked")
                finally:
                    release.set()
                dispatch_until(
                    lambda: not app.voice_models.snapshot()["busy"] and dialog.voice_action.get_label() == "Download",
                    "the cancelled voice download did not return to Download",
                )
                assert not app.voice_models.snapshot()["downloaded"]
                assert dialog.voice_action.get_label() == "Download"
            server.mode = "truncate"
            dialog.voice_action.emit("clicked")
            dispatch_until(
                lambda: not app.voice_models.snapshot()["busy"] and dialog.voice_action.get_label() == "Try again"
            )
            assert dialog.voice_action.get_label() == "Try again"
            assert "Recognise saved voices" in dialog.voice_line.get_label()
            server.mode = "normal"
            dialog.voice_action.emit("clicked")
            dispatch_until(
                lambda: (
                    not app.voice_models.snapshot()["busy"]
                    and dialog.voice_status.get_label() == "On"
                    and dialog.voice_action.get_label() == "Remove"
                )
            )
            assert dialog.voice_status.get_label() == "On" and dialog.voice_action.get_label() == "Remove"
            assert app.recording_settings.get_string("speech-model") == "whistle"
            assert (tmp / "models" / campplus_model.MODEL_ID / campplus_model.REVISION).is_dir()
            app.voice_models._change(ready=False, error=True, runtime_error=True)
            dispatch_until(lambda: dialog.voice_action.get_label() == "Try again")
            assert dialog.voice_remove.get_visible() and not dialog.voice_line.get_visible()
            retry_with_delayed_ready_notification(app, dialog)
            dialog.voice_action.emit("clicked")
            dispatch_until(
                lambda: not app.voice_models.snapshot()["busy"] and dialog.voice_action.get_label() == "Download"
            )
            assert dialog.voice_status.get_label() == "Not downloaded"
            assert dialog.voice_action.get_label() == "Download"
            assert not (tmp / "models" / campplus_model.MODEL_ID / campplus_model.REVISION).exists()
            dialog.force_close()
            app.begin_exit()
            window.close()
            app.do_shutdown()
            assert "onnxruntime" not in sys.modules and "cassini_talk.campplus" not in sys.modules
            print("PASS CAM++ GTK: Download/Cancel/failure/Try again/On/Remove; no engine in GTK")
finally:
    fixture.close()
