# SPDX-License-Identifier: GPL-3.0-or-later
"""D112 (L-87) "When was this recorded?": set a meeting's date and start time.

The file stat for the prefill and the save both run off the GTK thread.
"""

import logging
import threading

from gi.repository import Adw, GLib, Gtk

from . import calendar_formats
from .meeting_date import changes, prefill, saved_ms, set_started_at, started_at
from .strings import text

LOG = logging.getLogger(__name__)


class SetDateDialog(Adw.Dialog):
    def __init__(self, window, meeting):
        super().__init__(title=text("meeting.setDateTime.title"), content_width=360)
        self.window, self.meeting = window, meeting
        self.app = window.get_application()
        self.closed = self.busy = self.failed = self.edited = self.ready = False
        self._filling = False
        self.future = None
        body = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=18,
            margin_start=24,
            margin_end=24,
            margin_top=24,
            margin_bottom=24,
        )
        body.append(Gtk.Label(label=text("meeting.setDateTime.title"), css_classes=["title-2"], wrap=True))
        group = Adw.PreferencesGroup()
        self.calendar = Gtk.Calendar()
        popover = Gtk.Popover(child=self.calendar)
        self.date_button = Gtk.MenuButton(popover=popover, valign=Gtk.Align.CENTER, css_classes=["flat"])
        self.date_row = Adw.ActionRow(title=text("meeting.setDateTime.dateLabel"), activatable_widget=self.date_button)
        self.date_row.add_suffix(self.date_button)
        group.add(self.date_row)
        self.hour = self._spin(23)
        self.minute = self._spin(59)
        clock = Gtk.Box(spacing=4, valign=Gtk.Align.CENTER)
        clock.append(self.hour)
        clock.append(Gtk.Label(label=":"))
        clock.append(self.minute)
        self.time_row = Adw.ActionRow(title=text("meeting.setDateTime.timeLabel"), activatable_widget=self.hour)
        self.time_row.add_suffix(clock)
        group.add(self.time_row)
        body.append(group)
        self.hint = Gtk.Label(
            label=text("meeting.setDateTime.prefillSavedHint"),
            wrap=True,
            xalign=0,
            visible=False,
            css_classes=["dim-label", "caption"],
        )
        body.append(self.hint)
        buttons = Gtk.Box(spacing=12, homogeneous=True)
        self.cancel = Gtk.Button(label=text("common.cancel"))
        self.cancel.connect("clicked", lambda *_: self.close())
        buttons.append(self.cancel)
        # D112 names no Save id of its own; speakers.saveName is the shared "Save" button (all four apps).
        self.confirm = Gtk.Button(label=text("speakers.saveName"), css_classes=["suggested-action"])
        self.confirm.connect("clicked", self._save)
        buttons.append(self.confirm)
        body.append(buttons)
        self.set_child(body)
        self.set_default_widget(self.confirm)
        self.calendar.connect("day-selected", self._day_selected)
        for spin in (self.hour, self.minute):
            spin.connect("value-changed", self._changed)
        self.connect("closed", self._closed)
        self._fill(prefill(meeting, tz=window.tz, now_ms=window.now_ms))
        if meeting.started_at_ms is None and meeting.recorded_at_local is None:
            threading.Thread(target=self._read_saved_time, args=(meeting,), daemon=True).start()

    @staticmethod
    def _spin(upper):
        spin = Gtk.SpinButton.new_with_range(0, upper, 1)
        spin.set_wrap(True)
        spin.set_numeric(True)
        spin.set_width_chars(2)
        spin.set_orientation(Gtk.Orientation.VERTICAL)
        spin.connect("output", lambda button: button.set_text(f"{int(button.get_value()):02d}") or True)
        return spin

    def _read_saved_time(self, meeting):
        saved = saved_ms(meeting)
        GLib.idle_add(self._saved_time, meeting, saved)

    def _saved_time(self, meeting, saved):
        if not self.closed and not self.edited and saved is not None and meeting is self.meeting:
            self._fill(prefill(meeting, tz=self.window.tz, now_ms=self.window.now_ms, saved=saved))
        return GLib.SOURCE_REMOVE

    def _fill(self, value):
        self._filling = True
        try:
            day = GLib.DateTime.new_local(value.at.year, value.at.month, value.at.day, 12, 0, 0)
            # GTK 4.20 renames select_day to set_date; Ubuntu 24.04 has only select_day.
            (self.calendar.set_date if hasattr(self.calendar, "set_date") else self.calendar.select_day)(day)
            self.hour.set_value(value.at.hour)
            self.minute.set_value(value.at.minute)
        finally:
            self._filling = False
        self.hint.set_visible(value.from_file)
        # A prefill that is not the meeting's own start can be saved as it is.
        self.ready = value.from_file or self.meeting.started_at_ms is None and self.meeting.recorded_at_local is None
        self._show_date()
        self._validate()

    def _date(self):
        date = self.calendar.get_date()
        return date.get_year(), date.get_month(), date.get_day_of_month()

    def _show_date(self):
        from datetime import date

        self.date_button.set_label(calendar_formats.full_date(date(*self._date())))

    def _day_selected(self, *_):
        self._show_date()
        self._changed()
        popover = self.date_button.get_popover()
        if popover is not None and not self._filling:
            popover.popdown()

    def _changed(self, *_):
        if self._filling:
            return
        self.edited = True
        self.failed = False
        self.hint.set_visible(False)
        self._validate()

    def value(self):
        return started_at(*self._date(), int(self.hour.get_value()), int(self.minute.get_value()), tz=self.window.tz)

    def _validate(self):
        try:
            changed = (self.edited or self.ready) and changes(self.meeting, self.value())
        except ValueError:
            changed = False
        self.confirm.set_sensitive(bool(changed) and not self.busy)
        self.confirm.set_label(text("common.tryAgain" if self.failed else "speakers.saveName"))

    def update(self, meeting):
        self.meeting = meeting
        self._validate()  # Never replace values while the person edits.

    def _save(self, *_):
        if self.closed or self.busy or self.app._quitting or not self.confirm.get_sensitive():
            return
        value = self.value()
        self.busy = True
        self.set_can_close(False)
        self.confirm.grab_focus()
        for widget in (self.date_button, self.hour, self.minute, self.cancel):
            widget.set_sensitive(False)
        self._validate()
        self.app.hold()
        try:
            self.future = self.app.comfort.editor.submit(set_started_at, self.meeting.directory, value)
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
        for widget in (self.date_button, self.hour, self.minute, self.cancel):
            widget.set_sensitive(True)
        self._validate()

    def _saved(self, future):
        try:
            future.result()
        except Exception:
            LOG.exception("Could not set the meeting's date and time")
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
