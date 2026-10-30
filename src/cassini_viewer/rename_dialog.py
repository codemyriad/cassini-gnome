# SPDX-License-Identifier: GPL-3.0-or-later
"""Native saved-meeting rename, with IO on the shared metadata editor."""

import logging

from gi.repository import Adw, GLib, Gtk

from .meeting_rename import rename_meeting, renames, title_value
from .strings import CATALOG, text

LOG = logging.getLogger(__name__)


class RenameDialog(Adw.Dialog):
    def __init__(self, window, meeting):
        super().__init__(title=text("meeting.rename.title"), content_width=420)
        self.window, self.meeting = window, meeting
        self.app = window.get_application()
        self.closed = self.busy = self.failed = False
        self.future = None
        body = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=18,
            margin_start=24,
            margin_end=24,
            margin_top=24,
            margin_bottom=24,
        )
        body.append(Gtk.Label(label=text("meeting.rename.title"), css_classes=["title-2"], wrap=True))
        group = Adw.PreferencesGroup()
        self.entry = Adw.EntryRow(title=text("meeting.rename.field"), text=meeting.title, activates_default=True)
        group.add(self.entry)
        body.append(group)
        self.problem = Gtk.Label(wrap=True, xalign=0, visible=False, css_classes=["warn-line"])
        body.append(self.problem)
        buttons = Gtk.Box(spacing=12, homogeneous=True)
        self.cancel = Gtk.Button(label=text("common.cancel"))
        self.cancel.connect("clicked", lambda *_: self.close())
        buttons.append(self.cancel)
        self.confirm = Gtk.Button(label=text("meeting.rename.confirm"), css_classes=["suggested-action"])
        self.confirm.connect("clicked", self._save)
        buttons.append(self.confirm)
        body.append(buttons)
        self.set_child(body)
        self.set_default_widget(self.confirm)
        self.entry.connect("changed", self._changed)
        self.entry.connect("entry-activated", self._save)
        window.protect_text_input(self.entry)
        self.connect("map", self._mapped)
        self.connect("closed", self._closed)
        self._validate()

    def _mapped(self, *_):
        self.entry.grab_focus()
        self.entry.select_region(0, -1)

    def _changed(self, *_):
        self.failed = False
        self.problem.set_visible(False)
        self._validate()

    def _validate(self):
        try:
            changed = renames(self.meeting, self.meeting.metadata, title_value(self.entry.get_text()))
        except ValueError:
            changed = False
        self.confirm.set_sensitive(changed and not self.busy)
        self.confirm.set_label(text("common.tryAgain" if self.failed else "meeting.rename.confirm"))

    def update(self, meeting):
        self.meeting = meeting
        self._validate()  # Never replace text while the person edits.

    def _save(self, *_):
        if self.closed or self.busy or self.app._quitting or not self.confirm.get_sensitive():
            return
        self.busy = True
        self.set_can_close(False)
        # Let GtkText receive focus-out before disabling its parent EntryRow.
        # Otherwise its cursor timer warns while an asynchronous save is pending.
        self.confirm.grab_focus()
        self.entry.set_sensitive(False)
        self.cancel.set_sensitive(False)
        self._validate()
        self.app.hold()
        try:
            self.future = self.app.comfort.editor.submit(rename_meeting, self.meeting.directory, self.entry.get_text())
        except RuntimeError:
            self._failed()
            self.app.release()
        else:
            self.future.add_done_callback(lambda future: GLib.idle_add(self._saved, future))

    def _failed(self):
        if self.closed:
            return
        self.busy = False
        self.failed = True
        self.set_can_close(True)
        self.entry.set_sensitive(True)
        self.cancel.set_sensitive(True)
        # Use shared catalog copy while the fuller failure line is published.
        self.problem.set_label(
            text("meeting.rename.failed") if "meeting.rename.failed" in CATALOG else text("common.tryAgain")
        )
        self.problem.set_visible(True)
        self._validate()

    def _saved(self, future):
        try:
            future.result()
        except Exception:
            LOG.exception("Could not rename the meeting")
            self._failed()
        else:
            if not self.closed:
                self.force_close()
            if not self.window.monitor._closed:
                self.window.monitor.refresh({self.meeting.directory})
        finally:
            self.app.release()
        return GLib.SOURCE_REMOVE

    def _closed(self, *_):
        self.closed = True
        self.window._editor_focus(self.entry, False)
