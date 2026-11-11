# SPDX-License-Identifier: GPL-3.0-or-later
"""D96 batch progress; dedicated serial worker keeps the Library monitor free."""

import logging
from concurrent.futures import CancelledError
from dataclasses import replace
from threading import Event

from gi.repository import Adw, GLib, Gtk

from . import import_feedback
from .batch_import import Result, import_files
from .strings import text

LOG = logging.getLogger(__name__)


class ImportDialog(Adw.Dialog):
    def __init__(self, window, paths):
        super().__init__(content_width=400, title=text("import.progress", done=1, total=max(1, len(paths))))
        self.window = window
        self.cancelled = Event()
        self.closed = False
        self._result = Result(total=max(1, len(paths)))
        self.set_can_close(False)
        self.connect("close-attempt", lambda *_: self.cancel())
        body = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=18,
            margin_start=24,
            margin_end=24,
            margin_top=24,
            margin_bottom=24,
        )
        body.append(Gtk.Spinner(spinning=True, halign=Gtk.Align.CENTER))
        self.status = Gtk.Label(label=self.get_title(), wrap=True, accessible_role=Gtk.AccessibleRole.STATUS)
        body.append(self.status)
        self.cancel_button = Gtk.Button(label=text("common.cancel"), halign=Gtk.Align.CENTER)
        self.cancel_button.connect("clicked", lambda *_: self.cancel())
        body.append(self.cancel_button)
        self.set_child(body)
        window._opening_files += 1
        window._import_dialogs.add(self)
        self.future = window._import_executor.submit(self._run, paths)
        self.future.add_done_callback(lambda future: GLib.idle_add(self._done, future))

    def _run(self, paths):
        # A queued dialog appears only when its job starts. A new Open event
        # never hides another active import or runs another decoder beside it.
        if not self.cancelled.is_set():
            GLib.idle_add(self._show)
        return import_files(paths, cancel=self.cancelled, progress=lambda result: GLib.idle_add(self._progress, result))

    def _show(self):
        if not self.closed and not self.window.monitor._closed:
            self.present(self.window)
        return GLib.SOURCE_REMOVE

    def _progress(self, result):
        self._result = result
        if not self.closed and not self.window.monitor._closed:
            value = import_feedback.progress(result)
            self.status.set_label(value)
            self.set_title(value)
        return GLib.SOURCE_REMOVE

    def cancel(self):
        self.cancelled.set()
        if not self.closed:
            self.closed = True
            self.force_close()

    def _done(self, future):
        self.window._opening_files -= 1
        self.window._import_dialogs.discard(self)
        self.closed = True
        self.force_close()
        if self.window.monitor._closed:
            return GLib.SOURCE_REMOVE
        try:
            result = future.result()
        except CancelledError:
            result = replace(self._result, cancelled=True)
        except Exception:
            LOG.exception("Batch import failed")
            result = replace(self._result, failed=self._result.failed + 1)
        if result.last_key:
            if self.window.meeting_page or self.window.recording_page or self.window.search_page:
                self.window.back_to_library()
            self.window._pending_selection = result.last_key
            self.window.monitor.refresh_now()
        self.window.toast(import_feedback.summary(result))
        return GLib.SOURCE_REMOVE
