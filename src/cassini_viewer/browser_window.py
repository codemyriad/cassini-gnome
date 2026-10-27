# SPDX-License-Identifier: GPL-3.0-or-later
"""Discover and control browser recordings from the GNOME app."""

import json
from concurrent.futures import ThreadPoolExecutor
from gettext import gettext as _
from pathlib import Path
from urllib.parse import urlsplit

from gi.repository import Adw, Gio, GLib, Gtk

from . import browser_service


class BrowserMeetingsWindow(Adw.ApplicationWindow):
    def __init__(self, application, *, service_path=None):
        super().__init__(
            application=application, title=_("Meetings in your browser"), default_width=680, default_height=620
        )
        self.service_path = service_path
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cassini-meetings-ui")
        self._pending = False
        self._closed = False
        self._updating = False
        self._signature = None
        self.rows = {}
        view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        native = Gtk.Button(label=_("Record this computer…"), action_name="app.record")
        header.pack_start(native)
        view.add_top_bar(header)
        content = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=16,
            margin_start=24,
            margin_end=24,
            margin_top=18,
            margin_bottom=24,
        )
        content.append(Gtk.Label(label=_("Meetings in your browser"), css_classes=["title-1"], xalign=0))
        self.status = Gtk.Label(label=_("Connecting to browser…"), wrap=True, xalign=0)
        content.append(self.status)
        preferences = Adw.PreferencesGroup()
        self.automatic = Adw.SwitchRow(
            title=_("Record every browser meeting automatically"),
            subtitle=_(
                "Cassini records meetings you join later, even when this window is closed. Recordings stay on this computer. You can stop any recording. One meeting at a time per browser."
            ),
        )
        self.automatic.connect("notify::active", self._automatic_changed)
        preferences.add(self.automatic)
        content.append(preferences)
        self.error = Gtk.Label(wrap=True, xalign=0, css_classes=["error"], visible=False)
        content.append(self.error)
        self.list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"])
        self.empty = Adw.StatusPage(
            title=_("No meetings found"),
            icon_name="audio-input-microphone-symbolic",
            description=_("Open a meeting in your browser with the Cassini extension. Connected meetings appear here."),
        )
        self.stack = Gtk.Stack(vexpand=True)
        self.stack.add_named(self.empty, "empty")
        scroller = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroller.set_child(self.list)
        self.stack.add_named(scroller, "meetings")
        content.append(self.stack)
        self.saved = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"], visible=False)
        content.append(self.saved)
        view.set_content(content)
        self.set_content(view)
        self.connect("close-request", self._close)
        self._timer = GLib.timeout_add_seconds(1, self._refresh)
        self._refresh()

    def _refresh(self):
        if not self._pending and not self._closed:
            self._request("status")
        return GLib.SOURCE_CONTINUE

    def _request(self, action, **values):
        if self._closed or self._pending:
            return
        self._pending = True
        if action != "status":
            self.status.set_label(_("Saving recording…") if action == "stop" else _("Updating browser…"))
            self._set_controls(False)
        future = self._executor.submit(self._call, action, values)
        future.add_done_callback(lambda f: GLib.idle_add(self._received, f))

    def _call(self, action, values):
        target = browser_service.ensure_service(self.service_path)
        return browser_service.request(action, path=target, **values)

    def _received(self, future):
        self._pending = False
        if self._closed:
            return GLib.SOURCE_REMOVE
        self._set_controls(True)
        try:
            snapshot = future.result()
        except (OSError, ValueError) as error:
            self.error.set_label(str(error))
            self.error.set_visible(True)
            self.status.set_label(_("Browser connection unavailable"))
            return GLib.SOURCE_REMOVE
        self.error.set_visible(False)
        self._updating = True
        self.automatic.set_active(snapshot["autoRecord"])
        self._updating = False
        count = snapshot["browsers"]
        self.status.set_label(
            _("Listening inside your browser") if count else _("Waiting for a browser with the Cassini extension")
        )
        signature = json.dumps(snapshot, sort_keys=True)
        if signature != self._signature:
            self._signature = signature
            self._render(snapshot)
        return GLib.SOURCE_REMOVE

    def _set_controls(self, enabled):
        self.automatic.set_sensitive(enabled)
        for row in self.rows.values():
            row.button.set_sensitive(enabled and row.available)

    def _render(self, snapshot):
        while (child := self.list.get_first_child()) is not None:
            self.list.remove(child)
        self.rows = {}
        for meeting in snapshot["meetings"]:
            state = (
                _("Recording")
                if meeting["recording"]
                else (_("Audio available") if meeting["sourceCount"] else _("No audio yet"))
            )
            row = Adw.ActionRow(
                title=meeting["title"] or _("Browser meeting"),
                use_markup=False,
                subtitle=f"{urlsplit(meeting['url']).hostname or ''} · {state}",
            )
            row.button = Gtk.Button(
                label=_("Stop and save") if meeting["recording"] else _("Record"), valign=Gtk.Align.CENTER
            )
            row.button.add_css_class("destructive-action" if meeting["recording"] else "suggested-action")
            row.available = not meeting["busy"] and (
                meeting["recording"] or (not meeting["browserRecording"] and meeting["sourceCount"] > 0)
            )
            row.button.set_sensitive(row.available)
            action = "stop" if meeting["recording"] else "start"
            row.button.connect("clicked", lambda _button, a=action, i=meeting["id"]: self._request(a, id=i))
            row.add_suffix(row.button)
            if meeting.get("error"):
                row.set_subtitle(row.get_subtitle() + " · " + meeting["error"])
            self.list.append(row)
            self.rows[meeting["id"]] = row
        self.stack.set_visible_child_name("meetings" if self.rows else "empty")
        while (child := self.saved.get_first_child()) is not None:
            self.saved.remove(child)
        for recording in snapshot["recordings"][:3]:
            row = Adw.ActionRow(title=recording["title"], subtitle=recording["message"], use_markup=False)
            button = Gtk.Button(label=_("Open recording"), valign=Gtk.Align.CENTER)
            button.connect(
                "clicked",
                lambda _button, p=recording["path"]: self.get_application().open_in_new_window(
                    Gio.File.new_for_path(str(Path(p) / "session.json"))
                ),
            )
            row.add_suffix(button)
            self.saved.append(row)
        self.saved.set_visible(bool(snapshot["recordings"]))

    def _automatic_changed(self, *_args):
        if not self._updating:
            self._request("configure", autoRecord=self.automatic.get_active())

    def _close(self, *_args):
        self._closed = True
        GLib.source_remove(self._timer)
        self._executor.shutdown(wait=False, cancel_futures=True)
        return False
