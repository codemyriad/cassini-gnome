#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""About exposes downloaded notices through a local native launcher, without a fetch."""

import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

if os.environ.get("CASSINI_SCREENSHOT_ISOLATED") != "1" or os.environ.get("WAYLAND_DISPLAY"):
    raise SystemExit("Use build-aux/ux-screenshot.sh")
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gst", "1.0")
from gi.repository import Gio, GLib, Gtk  # noqa: E402

Gio.resources_register(Gio.Resource.load(str(Path(os.environ["CASSINI_TEST_BUILD"]) / "src/cassini-viewer.gresource")))
from cassini_viewer.main import CassiniApplication  # noqa: E402
from cassini_viewer.model_download import ModelStore, model_root  # noqa: E402


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
    assert check(), "About did not settle"


notice = model_root() / "NOTICE.txt"
notice.parent.mkdir(parents=True)
notice.write_text("Synthetic local model notices. No downloaded audio or transcript.\n")
app = CassiniApplication("com.gocassini.ModelNoticesTest", "test", "development")
app.register(None)
app.activate()
window = app.get_active_window()
until(lambda: not window._pending)
launched = []
with patch.object(ModelStore, "fetch_manifest", side_effect=AssertionError("About fetched a manifest")):
    about = app.on_about()
    with patch.object(
        Gtk.FileLauncher, "launch", lambda launcher, *_args: launched.append(launcher.get_file().get_path())
    ):
        assert about.emit("activate-link", notice.as_uri())
        assert launched == [str(notice)]
    about.force_close()
window.close()
app.do_shutdown()
print("PASS About: local NOTICE.txt native launch, no model network request")
