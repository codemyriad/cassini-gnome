#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""D35.3 real dialog keys, background portal, notification actions and holds."""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

if os.environ.get("CASSINI_SCREENSHOT_ISOLATED") != "1" or os.environ.get("WAYLAND_DISPLAY"):
    raise SystemExit("Use build-aux/ux-screenshot.sh")
if "--gtk-child" not in sys.argv:
    result = subprocess.run([sys.executable, __file__, "--gtk-child"], capture_output=True, text=True, timeout=70)
    print(result.stdout, end="")
    print(result.stderr, file=sys.stderr, end="")
    raise SystemExit(result.returncode or bool(re.search(r"Gtk-(WARNING|CRITICAL)", result.stderr)))
ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]
import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gst", "1.0")
gi.require_version("GdkX11", "4.0")
gi.require_version("Gsk", "4.0")
from desktop_services import DesktopServices  # noqa: E402
from gi.repository import GdkX11, Gio, GLib, Gtk  # noqa: E402

Gio.resources_register(Gio.Resource.load(str(Path(os.environ["CASSINI_TEST_BUILD"]) / "src/cassini-viewer.gresource")))
from cassini_viewer.background_recording import BackgroundRecording  # noqa: E402
from cassini_viewer.main import CassiniApplication  # noqa: E402


def until(check):
    end = time.monotonic() + 8
    while time.monotonic() < end:
        context = GLib.MainContext.default()
        context.iteration(False)
        for _ in range(64):
            if not context.pending():
                break
            context.iteration(False)
        if check():
            return
        time.sleep(0.005)
    raise AssertionError("Background condition timed out")


def widgets(widget):
    yield widget
    child = widget.get_first_child()
    while child:
        yield from widgets(child)
        child = child.get_next_sibling()


def press(dialog, label):
    button = next(w for w in widgets(dialog) if isinstance(w, Gtk.Button) and w.get_label() == label)
    until(lambda: button.get_mapped() and button.get_width() > 0)
    assert button.activate()


services = DesktopServices()
until(
    lambda: services.bus.call_sync(
        "org.freedesktop.DBus",
        "/org/freedesktop/DBus",
        "org.freedesktop.DBus",
        "NameHasOwner",
        GLib.Variant("(s)", ("org.freedesktop.portal.Desktop",)),
        GLib.VariantType.new("(b)"),
        Gio.DBusCallFlags.NONE,
        1000,
        None,
    ).unpack()[0]
)

# Wire contract: denial, immediate success, status bounds/deduplication and
# canceled pending requests. No device, display or network on the host is used.
permission = BackgroundRecording()
answers = []
services.background_denied = True
permission.request(answers.append)
until(lambda: answers == [False])
services.background_denied = False
permission.request(answers.append)
until(lambda: answers == [False, True])
requests = [c for c in services.calls if c["method"] == "RequestBackground"]
assert all(c["values"][1]["autostart"] is False and "commandline" not in c["values"][1] for c in requests)
permission.status("line\n" + "x" * 200)
until(lambda: any(c["method"] == "SetStatus" for c in services.calls))
status = [c for c in services.calls if c["method"] == "SetStatus"][-1]["values"][0]["message"]
assert len(status) == 96 and "\n" not in status
count = len(services.calls)
permission.status("line\n" + "x" * 200)
assert len(services.calls) == count
permission.request(answers.append)
assert answers == [False, True, True] and len(services.calls) == count
permission.close()
permission = BackgroundRecording()
services.background_defer = True
permission.request(answers.append)
until(lambda: bool(services.background_pending))
permission.close()
until(lambda: services.background_closed == 1)
assert answers == [False, True, True, False]
services.complete_background()  # Late permission cannot hide any window.
services.background_defer = False
services.background_error = True
unavailable = BackgroundRecording()
unavailable.request(answers.append)
until(lambda: answers == [False, True, True, False, False])
unavailable.close()
services.background_error = False

# Native registration uses the actual desktop ID and the same dedicated
# connection as its Background request, before that request is dispatched.
registered = BackgroundRecording("io.codemyriad.CassiniTalk", native=True)
registered.request(answers.append)
until(lambda: registered.granted)
registration = next(c for c in services.calls if c["method"] == "Register")
host_calls = [c for c in services.calls if c.get("sender") == registration["sender"]]
assert [c["method"] for c in host_calls] == ["Register", "RequestBackground"]
assert registration["values"] == ("io.codemyriad.CassiniTalk", {})
assert registration["sender"] != services.bus.get_unique_name()
registered.close()

with tempfile.TemporaryDirectory() as temporary:
    root = Path(temporary)
    schemas = root / "schemas"
    schemas.mkdir()
    shutil.copyfile(
        ROOT / "data/io.codemyriad.CassiniTalk.gschema.xml", schemas / "io.codemyriad.CassiniTalk.gschema.xml"
    )
    subprocess.run(["glib-compile-schemas", str(schemas)], check=True)
    os.environ.update(GSETTINGS_SCHEMA_DIR=str(schemas), CASSINI_LIBRARY_DIR=str(root / "Library"))
    Gtk.Settings.get_default().props.gtk_enable_animations = False
    app = CassiniApplication("com.gocassini.BackgroundTest", "test", "development")
    app.transcription_enabled = False
    app.register(None)
    assert app.background_recording.application_id == app.get_application_id()
    app.activate()
    window = app.get_active_window()
    window.set_default_size(1130, 730)
    until(lambda: not window._pending)
    app.start_native(test=True)
    until(
        lambda: (
            app.recorder
            and app.recorder.session
            and app.recorder.pipeline
            and all(t["frames"] >= 9600 for t in app.recorder.session.manifest["tracks"])
        )
    )
    native = app.recorder
    requests_before = len([c for c in services.calls if c["method"] == "RequestBackground"])
    window.close()
    dialog = app._capture_confirmation
    assert dialog.get_close_response() == "cancel"
    until(
        lambda: any(
            isinstance(w, Gtk.Button) and w.get_label() == "Cancel" and w.get_mapped() and w.get_width() > 0
            for w in widgets(dialog)
        )
    )
    ticks = []
    window.add_tick_callback(lambda *_: (ticks.append(True), len(ticks) < 3)[1])
    window.queue_draw()
    until(lambda: len(ticks) >= 3)
    paintable, snapshot = Gtk.WidgetPaintable.new(window), Gtk.Snapshot.new()
    paintable.snapshot(snapshot, window.get_width(), window.get_height())
    node = snapshot.to_node()
    texture = window.get_renderer().render_texture(node, node.get_bounds())
    assert (texture.get_width(), texture.get_height()) == (1120, 720)
    assert texture.save_to_png(os.environ["CASSINI_SCREENSHOT_OUT"])
    xid = GdkX11.X11Surface.get_xid(window.get_surface())
    subprocess.run(["xdotool", "windowfocus", "--sync", str(xid)], check=True, timeout=3)
    until(window.is_active)
    subprocess.run(["xdotool", "key", "--clearmodifiers", "Escape"], check=True, timeout=3)
    until(lambda: app._capture_confirmation is None)
    assert window.get_visible() and not app._capture_background and native.pipeline
    assert len([c for c in services.calls if c["method"] == "RequestBackground"]) == requests_before
    assert "recording" not in services.notifications
    window.close()
    press(app._capture_confirmation, "Cancel")
    until(lambda: app._capture_confirmation is None)
    assert window.get_visible() and not app._capture_background and native.pipeline
    services.background_denied = True
    window.close()
    press(app._capture_confirmation, "Keep Recording in Background")
    until(lambda: app._capture_confirmation is None)
    assert window.get_visible() and not app._capture_background and native.pipeline
    services.background_denied = False
    services.background_defer = True
    window.close()
    press(app._capture_confirmation, "Keep Recording in Background")
    until(lambda: bool(services.background_pending))
    assert window.get_visible() and not app._capture_background and native.pipeline
    sentinel = app._capture_confirmation
    window.close()
    assert app._capture_confirmation is sentinel, "Pending consent must not open another close dialog"
    app.stop_native()
    until(lambda: native.pipeline is None and not native._finishing)
    services.complete_background()
    until(lambda: app._capture_confirmation is None)
    assert window.get_visible() and not app._capture_background, "Late approval hid a finished capture"
    app.start_native(test=True)
    until(
        lambda: (
            app.recorder
            and app.recorder.session
            and app.recorder.pipeline
            and all(t["frames"] >= 9600 for t in app.recorder.session.manifest["tracks"])
        )
    )
    native = app.recorder
    window.close()
    press(app._capture_confirmation, "Keep Recording in Background")
    # The previous successful consent is retained even though that capture
    # finished before it arrived. Subsequent hides do not prompt again.
    until(lambda: app._capture_background and not window.get_visible() and "recording" in services.notifications)
    assert not services.background_pending
    notification = services.notifications["recording"]
    assert notification["title"] == "Cassini is recording"
    assert [b["label"] for b in notification["buttons"]] == ["Stop", "Show"]
    assert [b["action"] for b in notification["buttons"]] == ["app.stop-recording", "app.open-recording"]
    services.app_action(app, "open-recording")
    until(lambda: window.get_visible() and not app._capture_background and "recording" not in services.notifications)
    assert native.pipeline and not app._quitting
    services.background_defer = False
    window.close()
    press(app._capture_confirmation, "Keep Recording in Background")
    until(lambda: app._capture_background and "recording" in services.notifications)
    services.app_action(app, "stop-recording")
    until(lambda: native.pipeline is None and not native._finishing and not app._capture_background)
    app.comfort.tick()
    until(lambda: "recording" not in services.notifications)
    assert not app._quitting, "Background Stop must not cancel writing down"

    # Automatic recording alone keeps the app reachable. Reopening creates a
    # fresh Library; repeated closes reuse consent and the one-time notice.
    app.set_auto_recording_hold(True)
    window = app.get_active_window()
    if window is not None:
        window.close()
    # No manual tick: closing the window publishes windowless state on idle.
    until(lambda: "background" in services.notifications)
    assert services.notifications["background"]["title"] == "Cassini is still running"
    assert services.notifications["background"]["body"] == (
        "It records browser meetings automatically. To open it, start Cassini again."
    )
    assert app.background_recording.last_status == "Ready to record browser meetings"
    first_notice_count = sum(item["id"] == "background" for item in services.history)
    services.app_action(app, "open-recording")
    until(lambda: any(w.get_visible() for w in app.get_windows()))
    assert app.background_recording.last_status == ""
    app.get_active_window().close()
    app.comfort.tick()
    assert not any(w.get_visible() for w in app.get_windows())
    assert sum(item["id"] == "background" for item in services.history) == first_notice_count

    # This is the exact app.quit action used by GNOME Background Apps. It must
    # finalize audio and quit without presenting an invisible confirmation.
    app.start_native(test=True)
    until(lambda: app.recorder and app.recorder.pipeline and app.recorder.session.manifest["tracks"][0]["frames"] > 0)
    native = app.recorder
    services.app_action(app, "quit")
    until(lambda: app._quitting and not native.pipeline and not native._finishing)
    assert app._capture_confirmation is None
    assert any(native.session.directory.glob("*.wav"))
    app.background_recording.close()
    app.comfort.close()
services.close()
print("PASS background: real Escape/Cancel; denied/pending/allowed portal; Stop/Show notification; balanced holds")
