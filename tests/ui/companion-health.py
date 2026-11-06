#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""D72 private GTK: honest setup completion and extension-owned tab health."""

import os
import sys
from pathlib import Path
from types import SimpleNamespace

if os.environ.get("CASSINI_SCREENSHOT_ISOLATED") != "1" or os.environ.get("WAYLAND_DISPLAY"):
    raise SystemExit("Use build-aux/ux-screenshot.sh")
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402
from painted import painted_node  # noqa: E402

from cassini_talk.browser_connection import BrowserConnection  # noqa: E402
from cassini_talk.companion import Browser  # noqa: E402
from cassini_talk.companion_setup import CompanionSetup  # noqa: E402
from cassini_talk.strings import text  # noqa: E402


def until(check):
    context = GLib.MainContext.default()
    expired = []
    timer = GLib.timeout_add_seconds(5, lambda: expired.append(True) or False)
    try:
        while not check():
            assert not expired, "Health UI did not settle"
            context.iteration(True)
    finally:
        if not expired:
            GLib.source_remove(timer)


def labels(widget):
    result = [widget.get_label()] if isinstance(widget, (Gtk.Label, Gtk.Button)) else []
    child = widget.get_first_child()
    while child:
        result.extend(labels(child))
        child = child.get_next_sibling()
    return result


def health(state="idle", tabs=None):
    return dict(state=state, worker="ready", nativeControl="connected", tabs=tabs or [])


def peer(identifier="profile-1", check=True, version="0.2.20", value=None, caps=None, busy=None):
    return dict(
        id=identifier,
        browser="Chrome",
        diagnostics=dict(filesOk=check, extensionVersion=version),
        capabilities=caps if caps is not None else ["multi-stream-v1", "tab-readiness-v1"],
        browserHealth=value or health(),
        busyTabs=busy or [],
    )


app = Adw.Application(application_id="com.gocassini.HealthTest")
app.register(None)
window = Adw.ApplicationWindow(application=app, default_width=1000, default_height=900)
window.present()
holder = {}
controller = BrowserConnection(lambda: holder["dialog"].render() if "dialog" in holder else None)
controller.bundled_version = "0.2.20"
controller.browsers = (Browser("Chrome", "/bin/true", Path(os.environ["HOME"]) / "Chrome"),)
owner = SimpleNamespace(
    browser_connection=controller, browser_companion_available=True, retry_browser_connection=lambda **_: None
)
dialog = holder["dialog"] = CompanionSetup(owner)
dialog.present(window)
until(dialog.get_mapped)


def observe(peers, connected=True):
    controller.observe(
        dict(connectedBrowsers=int(connected), browserNames=["Chrome"] if connected else [], browserPeers=peers)
    )
    dialog.render()


observe([], False)
assert not dialog.done.get_sensitive() and not dialog.health_rows
observe([])
assert not dialog.done.get_sensitive() and text("setup.companion.setUpAgain") in labels(dialog.health_box)
observe([peer(check=False)])
assert not dialog.done.get_sensitive() and text("setup.companion.filesMissing") in labels(dialog.health_box)
observe([peer(check=None)])
assert not dialog.done.get_sensitive() and text("setup.companion.filesMissing") not in labels(dialog.health_box)
observe([peer()])
assert dialog.done.get_sensitive() and not dialog.health_rows and not dialog.health_scroller.get_visible()
observe([peer(version="0.2.18")])
assert dialog.done.get_sensitive(), "Version wording cannot gate recording capability or self-check"
assert text("setup.companion.outdated", version="0.2.18") in labels(dialog.health_box)
reload = dict(tabId=7, state="reloadRequired", action="reloadMeetingTab", hookSeen=True, relayAlive=False)
observe([peer(value=health("idle", [reload]))])
assert not dialog.health_rows and text("setup.companion.reloadTab") not in labels(dialog.health_box)
observe([peer(value=health("reloadRequired", [reload]))])
assert dialog.done.get_sensitive() and len(dialog.health_rows) == 1
browser, tab, caption, button = dialog.health_rows[0]
assert caption.get_label() == text("setup.companion.reloadTab") and button.get_label() == text(
    "setup.companion.reloadTab.action"
)
requested = []
dialog._reload_tab = lambda browser, tab: requested.append((browser, tab))
until(lambda: button.get_mapped() and button.get_sensitive() and button.get_width() > 0)
assert button.get_realized() and button.activate()
until(lambda: bool(requested))
assert requested == [("profile-1", 7)]
assert dialog.health_rows, "Clicking Reload cannot claim the page recovered"
observe([peer(value=health("reloadRequired", [reload]), busy=[7])])
assert not dialog.health_rows[0][3].get_sensitive()
observe([peer(value=health("reloadRequired", [reload]), caps=["multi-stream-v1"])])
assert not dialog.health_rows[0][3].get_sensitive()
observe([peer(), peer("profile-2", check=False, value=health("reloadRequired", [reload]))])
assert not dialog.done.get_sensitive() and dialog.health_rows[0][:2] == ("profile-2", 7)
for scheme, suffix in [(Adw.ColorScheme.FORCE_LIGHT, "light"), (Adw.ColorScheme.FORCE_DARK, "dark")]:
    Adw.StyleManager.get_default().set_color_scheme(scheme)
    frames = []
    window.add_tick_callback(lambda *_, frames=frames: (frames.append(True), len(frames) < 3)[1])
    window.queue_draw()
    until(lambda frames=frames: len(frames) >= 3)
    scroller = dialog.get_child()
    assert scroller.get_width() >= 540
    ok, done_bounds = dialog.done.compute_bounds(scroller)
    assert ok and done_bounds.get_y() >= 0 and done_bounds.get_y() + done_bounds.get_height() <= scroller.get_height()
    node = painted_node(window)
    texture = window.get_renderer().render_texture(node, node.get_bounds())
    assert texture.save_to_png(
        str(Path(os.environ["CASSINI_SCREENSHOT_OUT"]).with_name("companion-health-" + suffix + ".png"))
    )
observe([], False)
assert not dialog.done.get_sensitive() and not dialog.health_rows and not dialog.health_scroller.get_visible()
dialog.discard()
controller.close()
window.close()
print(
    "PASS companion health: connected + files, every profile, diagnostic version, idle silence, exact-tab explicit reload, busy guards, light/dark"
)
