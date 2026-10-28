# SPDX-License-Identifier: GPL-3.0-or-later
"""UX10 setup: installation and registration are not a connection handshake."""

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event

from gi.repository import Adw, GLib, Gtk

from . import browser_service, companion
from .strings import text

LOG = logging.getLogger(__name__)


class CompanionSetup(Adw.Dialog):
    def __init__(self, app, *, service_path=None):
        super().__init__(title=text("companion.title"), content_width=540, css_classes=["companion-setup"])
        self.app, self.service_path = app, service_path
        self.closed = False
        self.cancelled = Event()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cassini-companion")
        self.future = None
        self.connect_requested = self.working = self.failed = self.add_requested = False
        self.deadline = None
        self.browser_names = []
        self.connected = False
        content = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=20,
            margin_start=26,
            margin_end=26,
            margin_top=26,
            margin_bottom=22,
        )
        self.set_child(content)
        heading = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        heading.append(Gtk.Label(label=text("companion.title"), wrap=True, css_classes=["title-2"]))
        heading.append(
            Gtk.Label(
                label=text("companion.body"), wrap=True, justify=Gtk.Justification.CENTER, css_classes=["dim-label"]
            )
        )
        content.append(heading)
        steps = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"])
        self.add_row, self.add_badge, self.add_caption, self.add_button = self._step(
            1, "companion.addStep.title", "companion.addStep.notAdded", "companion.add", self._add
        )
        self.connect_row, self.connect_badge, self.connect_caption, self.connect_button = self._step(
            2, "companion.connectStep.title", "companion.connectStep.caption", "companion.connect", self._connect
        )
        self.connect_button.add_css_class("suggested-action")
        steps.append(self.add_row)
        steps.append(self.connect_row)
        content.append(steps)
        privacy = Gtk.Box(spacing=10)
        privacy.append(
            Gtk.Image(
                icon_name="changes-prevent-symbolic",
                valign=Gtk.Align.START,
                accessible_role=Gtk.AccessibleRole.PRESENTATION,
            )
        )
        privacy.append(
            Gtk.Label(
                label=text("companion.privacy"), xalign=0, wrap=True, hexpand=True, css_classes=["caption", "dim-label"]
            )
        )
        content.append(privacy)
        actions = Gtk.Box(spacing=10, homogeneous=True)
        later = Gtk.Button(label=text("common.notNow"))
        later.connect("clicked", lambda *_: self.close())
        actions.append(later)
        self.done = Gtk.Button(label=text("common.done"), sensitive=False)
        self.done.connect("clicked", lambda *_: self.close())
        actions.append(self.done)
        content.append(actions)
        self.set_default_widget(self.connect_button)
        self.connect("closed", self._closed)
        self.timer = GLib.timeout_add(500, self.poll)
        self.render()
        self.poll()

    @staticmethod
    def _step(number, title, caption, action, callback):
        row = Gtk.ListBoxRow(activatable=False)
        line = Gtk.Box(spacing=14, margin_start=16, margin_end=16, margin_top=14, margin_bottom=14)
        badge = Gtk.Label(
            label=str(number),
            valign=Gtk.Align.CENTER,
            css_classes=["companion-step-badge"],
            accessible_role=Gtk.AccessibleRole.PRESENTATION,
        )
        line.append(badge)
        labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3, hexpand=True)
        labels.append(Gtk.Label(label=text(title), xalign=0, wrap=True, css_classes=["heading"]))
        detail = Gtk.Label(label=text(caption), xalign=0, wrap=True, css_classes=["caption", "dim-label"])
        labels.append(detail)
        line.append(labels)
        button = Gtk.Button(label=text(action), valign=Gtk.Align.CENTER)
        button.connect("clicked", callback)
        line.append(button)
        row.set_child(line)
        return row, badge, detail, button

    def _add(self, *_):
        self.add_requested = True
        self.poll()

    def _launch_add(self):
        browsers = companion.installed_browsers()
        if not browsers:
            raise OSError("No supported browser is installed")
        browser = next((b for b in browsers if b.name in self.browser_names), browsers[0])
        companion.open_add(browser)

    def _connect(self, *_):
        self.connect_requested = self.working = True
        self.failed = False
        self.deadline = None
        self.render()
        self.poll()

    def _submit(self, call, action):
        if self.future is not None or self.closed:
            return False
        self.future = self.executor.submit(call)
        self.future.add_done_callback(lambda future: GLib.idle_add(self._received, future, action))
        return True

    def _snapshot(self):
        target = browser_service.ensure_service(self.service_path, cancelled=self.cancelled)
        with browser_service.connect(target, timeout=0.8) as connection, connection.makefile("rwb") as stream:
            browser_service.send_packet(stream, dict(role="app", action="status"))
            result = browser_service.read_packet(stream)
            if result is None or result.get("error"):
                raise OSError("Browser connection unavailable")
            return result

    def poll(self):
        if self.closed:
            return GLib.SOURCE_REMOVE
        if self.connect_requested and self.future is None:
            self.connect_requested = False
            self._submit(companion.register, "register")
        elif self.add_requested and self.future is None:
            self.add_requested = False
            self._submit(self._launch_add, "add")
        else:
            self._submit(self._snapshot, "status")
        if self.deadline is not None and time.monotonic() >= self.deadline and not self.connected:
            self.working, self.failed, self.deadline = False, True, None
            self.render()
        return GLib.SOURCE_CONTINUE

    def _received(self, future, action):
        self.future = None
        if self.closed:
            return False
        try:
            value = future.result()
            if action == "status":
                self.connected = bool(value.get("connectedBrowsers", value.get("browsers")))
                names = value.get("browserNames", [])
                if names:
                    self.browser_names = list(names)
                if self.connected:
                    self.working = self.failed = False
                    self.deadline = None
            elif action == "register":
                self.deadline = time.monotonic() + 35
        except Exception:
            LOG.exception("Companion %s failed", action)
            if action != "status":
                self.working, self.failed, self.deadline = False, True, None
        self.render()
        if self.connect_requested or self.add_requested:
            self.poll()
        return False

    def render(self):
        added = self.connected or bool(self.browser_names)
        if self.browser_names:
            key = "companion.addStep.doneSeveral" if len(self.browser_names) > 1 else "companion.addStep.done"
            self.add_caption.set_label(text(key, browser=self.browser_names[0], browsers=", ".join(self.browser_names)))
        else:
            self.add_caption.set_label(
                text("settings.browser.status.connectedGeneric") if added else text("companion.addStep.notAdded")
            )
        self.add_button.set_visible(not added)
        if self.connected:
            caption = "companion.connectStep.done"
        elif self.working:
            caption = "companion.connectStep.connecting"
        elif self.failed:
            caption = "companion.connectStep.failed"
        else:
            caption = "companion.connectStep.caption"
        self.connect_caption.set_label(text(caption))
        self.connect_button.set_visible(not self.connected)
        self.connect_button.set_sensitive(not self.working)
        self.connect_button.set_label(text("common.tryAgain" if self.failed else "companion.connect"))
        self.done.set_sensitive(self.connected)
        self.set_default_widget(self.done if self.connected else self.connect_button)
        for n, row, badge, done, working in (
            (1, self.add_row, self.add_badge, added, False),
            (2, self.connect_row, self.connect_badge, self.connected, self.working),
        ):
            badge.set_label("✓" if done else str(n))
            badge.set_css_classes(["companion-step-badge"] + (["done"] if done else []))
            state = "done" if done else "working" if working else "pending"
            row.update_property([Gtk.AccessibleProperty.LABEL], [text("companion.step.a11y." + state, n=n)])

    def _closed(self, *_):
        self.closed = True
        self.cancelled.set()
        if self.timer:
            GLib.source_remove(self.timer)
            self.timer = 0
        self.executor.shutdown(wait=False, cancel_futures=True)
