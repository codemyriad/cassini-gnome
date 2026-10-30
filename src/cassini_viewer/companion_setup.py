# SPDX-License-Identifier: GPL-3.0-or-later
"""UX10 setup: installation and registration are not a connection handshake."""

import logging
from concurrent.futures import ThreadPoolExecutor
from threading import Event

from gi.repository import Adw, GLib, Gtk

from . import browser_service, companion
from .browser_connection import add_text, classify, state_text
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
        self.working = self.failed = self.add_requested = False
        self.local_error = None
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
            2, "companion.connectStep.title", "browser.state.settingUp", "common.tryAgain", self._connect
        )
        self.add_retry = Gtk.Button(label=text("common.tryAgain"), valign=Gtk.Align.CENTER, visible=False)
        self.add_retry.connect("clicked", self._connect)
        self.add_row.get_child().append(self.add_retry)
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
        self.local_error = None
        self.app.retry_browser_connection()
        self.render()

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
        if self.add_requested and self.future is None:
            self.add_requested = False
            self._submit(self._launch_add, "add")
        elif self.service_path is not None:
            self._submit(self._snapshot, "status")
        self.render()
        return GLib.SOURCE_CONTINUE

    def _received(self, future, action):
        self.future = None
        if self.closed:
            return False
        try:
            value = future.result()
            if action == "status" and self.app.browser_connection:
                self.app.browser_connection.observe(value)
            elif action == "add":
                self.local_error = None
        except Exception as error:
            LOG.exception("Companion %s failed", action)
            if action != "status":
                self.local_error = classify(error)
        self.render()
        if self.add_requested:
            self.poll()
        return False

    def render(self):
        controller = self.app.browser_connection
        state = controller.snapshot() if controller else dict(phase="settingUp", names=(), browser=None)
        if state["phase"] == "connected":
            self.local_error = None
        if self.local_error:
            state = dict(state, phase="failed", reason=self.local_error.reason)
        self.connected = state["phase"] == "connected"
        self.working, self.failed = state["phase"] == "settingUp", state["phase"] == "failed"
        if self.connected and state["names"]:
            self.browser_names = list(state["names"])
        added = self.connected or bool(self.browser_names)
        if self.browser_names:
            key = "companion.addStep.doneSeveral" if len(self.browser_names) > 1 else "companion.addStep.done"
            self.add_caption.set_label(text(key, browser=self.browser_names[0], browsers=", ".join(self.browser_names)))
        else:
            self.add_caption.set_label(
                text("settings.browser.status.connectedGeneric") if added else text("companion.addStep.notAdded")
            )
        self.add_button.set_visible(not added)
        self.add_button.set_sensitive(state["browser"] is not None and self.future is None)
        self.add_button.set_label(add_text(state))
        self.connect_row.set_visible(added)
        self.add_retry.set_visible(not added and self.failed and self.app.browser_companion_available)
        if not added and self.failed:
            self.add_caption.set_label(state_text(state))
        self.connect_caption.set_label(state_text(state) if added else "")
        self.connect_caption.set_visible(added)
        if state["phase"] == "needsBrowser":
            self.add_caption.set_label(state_text(state))
        self.connect_button.set_visible(added and self.failed and self.app.browser_companion_available)
        self.connect_button.set_sensitive(not self.working)
        self.connect_button.set_label(text("common.tryAgain"))
        self.done.set_sensitive(self.connected)
        self.set_default_widget(
            self.done
            if self.connected
            else self.connect_button
            if self.connect_button.get_visible()
            else self.add_retry
            if self.add_retry.get_visible()
            else self.add_button
            if self.add_button.get_visible()
            else None
        )
        for n, row, badge, done, working in (
            (1, self.add_row, self.add_badge, added, False),
            (2, self.connect_row, self.connect_badge, self.connected, added and self.working),
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
