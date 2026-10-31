#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""About exposes downloaded notices through a local native launcher, without a fetch."""

import json
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

Gio.resources_register(Gio.Resource.load(str(Path(os.environ["CASSINI_TEST_BUILD"]) / "src/cassini-talk.gresource")))
from cassini_talk.main import CassiniApplication  # noqa: E402
from cassini_talk.model_download import ModelStore, model_root  # noqa: E402


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


def labels(widget):
    values = [widget.get_text()] if isinstance(widget, Gtk.Label) else []
    child = widget.get_first_child()
    while child:
        values += labels(child)
        child = child.get_next_sibling()
    return values


launched = []
with patch.object(ModelStore, "fetch_manifest", side_effect=AssertionError("About fetched a manifest")):
    empty = app.on_about()
    assert "Licences of downloaded models" not in labels(empty)
    empty.force_close()
    revision = model_root() / "fixture-model" / ("a" * 64)
    revision.mkdir(parents=True)
    (revision / "weights.bin").write_bytes(b"synthetic private fixture")
    (revision / ".cassini-model.json").write_text(
        json.dumps(
            dict(
                id="fixture-model",
                source_sha256="a" * 64,
                files=[dict(path="weights.bin")],
            )
        )
    )
    about = app.on_about()
    assert "Licences of downloaded models" in labels(about)
    with patch.object(
        Gtk.FileLauncher, "launch", lambda launcher, *_args: launched.append(launcher.get_file().get_path())
    ):
        assert about.emit("activate-link", notice.as_uri())
        assert launched == [str(notice)]
    about.force_close()
    ModelStore().remove("fixture-model", "a" * 64)
    assert notice.is_file()  # A kept notice alone must not expose the link.
    removed = app.on_about()
    assert "Licences of downloaded models" not in labels(removed)
    removed.force_close()
window.close()
app.do_shutdown()
print("PASS About: local NOTICE.txt native launch, no model network request")
