# SPDX-License-Identifier: GPL-3.0-or-later
"""D96 bulk stored-.zip selector and progress dialog."""

import errno
import os
import threading
from concurrent.futures import CancelledError, ThreadPoolExecutor
from pathlib import Path

from gi.repository import Adw, Gio, GLib, Gtk

from .i18n import gettext as _
from .library_window import label, vertical
from .strings import plural, text
from .zip_export import export_meetings_zip, exportable_meetings


class ZipExportDialog(Adw.Dialog):
    def __init__(self, window):
        super().__init__(title=text("export.zip.all"), content_width=520)
        self.add_css_class("export-dialog")
        self.set_can_close(False)
        self.connect("close-attempt", self.cancel)
        self.window = window
        app = window.get_application()
        self.settings = app.recording_settings
        uri = self.settings.get_string("export-folder-uri")
        self.destination = Gio.File.new_for_uri(uri) if uri else None
        self.suggested_folder = Gio.File.new_for_path(
            GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOCUMENTS) or str(Path.home() / "Documents")
        )
        self.choosing = False
        self.folder_cancel = None
        self.success_toast = None
        self.cancelled = threading.Event()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cassini-zip-export")
        self.closed = False
        self.busy = False
        self.future = None
        self.output = None
        self.active_directories = frozenset()
        self._syncing_checks = False
        self.choices = exportable_meetings(window.snapshot, trash=app.meeting_trash)
        self.selected_keys = [meeting.key for meeting in self.choices]
        self._choices_signature = None
        self.checks = {}
        self.choice_rows = []

        self.stack = Gtk.Stack()
        select_page = vertical(18, margin_start=28, margin_end=28, margin_top=24, margin_bottom=24)
        self.heading = label(self.get_title(), "export-heading", wrap=True, justify=Gtk.Justification.CENTER)
        select_page.append(self.heading)

        self.export_all_button = Gtk.Button(label=text("export.zip.all"), halign=Gtk.Align.START)
        self.export_all_button.connect("clicked", self._export_all)
        select_page.append(self.export_all_button)

        self.choices_box = vertical(8, css_classes=["export-choices"], accessible_role=Gtk.AccessibleRole.GROUP)
        self.choices_box.update_property([Gtk.AccessibleProperty.LABEL], [text("export.zip.all")])
        select_page.append(
            Gtk.ScrolledWindow(
                child=self.choices_box,
                hscrollbar_policy=Gtk.PolicyType.NEVER,
                propagate_natural_height=True,
                max_content_height=340,
            )
        )

        buttons = Gtk.Box(spacing=10, homogeneous=True)
        self.cancel_button = Gtk.Button(label=text("common.cancel"))
        self.cancel_button.connect("clicked", self.cancel)
        buttons.append(self.cancel_button)
        self.export_selected_button = Gtk.Button(
            label=text("export.zip.selected"),
            css_classes=["suggested-action"],
        )
        self.export_selected_button.connect("clicked", self._export_selected)
        buttons.append(self.export_selected_button)
        select_page.append(buttons)
        self.stack.add_named(select_page, "select")

        progress_page = vertical(18, margin_start=24, margin_end=24, margin_top=24, margin_bottom=24)
        progress_page.append(Gtk.Spinner(spinning=True, halign=Gtk.Align.CENTER))
        self.progress_label = Gtk.Label(
            label=text("export.zip.progress", done=1, total=max(1, len(self.choices))),
            wrap=True,
            accessible_role=Gtk.AccessibleRole.STATUS,
        )
        progress_page.append(self.progress_label)
        self.progress_cancel_button = Gtk.Button(label=text("common.cancel"), halign=Gtk.Align.CENTER)
        self.progress_cancel_button.connect("clicked", self.cancel)
        progress_page.append(self.progress_cancel_button)
        self.stack.add_named(progress_page, "progress")

        self.stack.set_visible_child_name("select")
        self.set_child(self.stack)
        self.connect("closed", self._closed)
        self._rebuild_choices()
        self._sync_controls()

    def _rebuild_choices(self):
        signature = tuple((meeting.key, meeting.title) for meeting in self.choices)
        if signature == self._choices_signature:
            self._sync_check_states()
            return
        self._choices_signature = signature
        while child := self.choices_box.get_first_child():
            self.choices_box.remove(child)
        self.checks = {}
        self.choice_rows = []
        selected = set(self.selected_keys)
        for meeting in self.choices:
            check = Gtk.CheckButton(active=meeting.key in selected)
            check.update_property([Gtk.AccessibleProperty.LABEL], [meeting.title])
            row = Gtk.Box(spacing=12, css_classes=["export-choice"])
            if meeting.key in selected:
                row.add_css_class("selected")
            row.append(check)
            row.append(label(meeting.title, hexpand=True, xalign=0, wrap=True))
            check.connect("toggled", self._toggled, meeting.key, row)
            click = Gtk.GestureClick()
            click.connect(
                "released", lambda _gesture, _n, _x, _y, check=check: check.set_active(not check.get_active())
            )
            row.add_controller(click)
            self.choices_box.append(row)
            self.checks[meeting.key] = check
            self.choice_rows.append(row)

    def _sync_check_states(self):
        selected = set(self.selected_keys)
        self._syncing_checks = True
        try:
            for key, check in self.checks.items():
                active = key in selected
                if check.get_active() != active:
                    check.set_active(active)
        finally:
            self._syncing_checks = False

    def _toggled(self, check, key, row):
        active = check.get_active()
        (row.add_css_class if active else row.remove_css_class)("selected")
        if self._syncing_checks:
            return
        if active:
            if key not in self.selected_keys:
                order = {meeting.key: index for index, meeting in enumerate(self.choices)}
                self.selected_keys = sorted([*self.selected_keys, key], key=lambda item: order.get(item, 0))
        else:
            self.selected_keys = [item for item in self.selected_keys if item != key]
        self._sync_controls()

    def _sync_controls(self):
        idle = not self.busy and not self.choosing and not self.closed
        self.export_all_button.set_sensitive(idle and bool(self.choices))
        self.export_selected_button.set_visible(bool(self.selected_keys))
        self.export_selected_button.set_sensitive(idle and bool(self.selected_keys))
        for check in self.checks.values():
            check.set_sensitive(idle)

    def update_snapshot(self, snapshot):
        if self.busy or self.closed:
            return
        app = self.window.get_application()
        self.choices = exportable_meetings(snapshot, trash=app.meeting_trash if app else None)
        valid = {meeting.key for meeting in self.choices}
        self.selected_keys = [key for key in self.selected_keys if key in valid]
        self._rebuild_choices()
        self._sync_controls()

    def _export_all(self, *_):
        if self.busy or self.choosing or self.closed or not self.choices:
            return
        self.selected_keys = [meeting.key for meeting in self.choices]
        self._sync_check_states()
        self._sync_controls()
        self._choose(all_items=True)

    def _export_selected(self, *_):
        if self.busy or self.choosing or self.closed or not self.selected_keys:
            return
        self._choose(all_items=False)

    def _choose(self, *, all_items):
        self.choosing = True
        self._sync_controls()
        self.folder_cancel = Gio.Cancellable()
        dialog = Gtk.FileDialog(title=text("common.choose"), initial_folder=self.destination or self.suggested_folder)
        dialog.select_folder(
            self.window,
            self.folder_cancel,
            lambda chooser, result: self._chosen(chooser, result, all_items=all_items),
        )

    def _chosen(self, dialog, result, *, all_items):
        self.choosing = False
        self.folder_cancel = None
        if not self.closed:
            self._sync_controls()
        try:
            folder = dialog.select_folder_finish(result)
        except GLib.Error as error:
            if not self.closed and not (
                error.matches(Gtk.dialog_error_quark(), Gtk.DialogError.DISMISSED)
                or error.matches(Gtk.dialog_error_quark(), Gtk.DialogError.CANCELLED)
                or error.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED)
            ):
                self._failed(error)
            return
        if self.closed or self.cancelled.is_set() or folder is None:
            return
        if folder.get_path() is None:
            self._failed(OSError(errno.ENOTSUP, os.strerror(errno.ENOTSUP)))
            return
        self.destination = folder
        self.settings.set_string("export-folder-uri", folder.get_uri())
        self._start_export(Path(folder.get_path()), all_items=all_items)

    def _start_export(self, destination_path, *, all_items):
        if self.busy or self.closed:
            return
        app = self.window.get_application()
        trash = app.meeting_trash if app else None
        current = exportable_meetings(self.window.snapshot, trash=trash)
        if all_items:
            targets = current
        else:
            wanted = set(self.selected_keys)
            targets = tuple(meeting for meeting in current if meeting.key in wanted)
        if not targets:
            self._failed(ValueError("No meetings selected for export"))
            return
        self.busy = True
        self._target_count = len(targets)
        self.active_directories = frozenset(meeting.directory.absolute() for meeting in targets)
        self._sync_controls()
        initial = text("export.zip.progress", done=1, total=len(targets))
        self.progress_label.set_label(initial)
        self.set_title(initial)
        self.stack.set_visible_child_name("progress")
        self.window._refresh_export()
        self.future = self.executor.submit(
            export_meetings_zip,
            targets,
            destination_path,
            tz=self.window.tz,
            cancel=self.cancelled,
            progress=lambda done, total: GLib.idle_add(self._progress, done, total),
            is_trashed=trash.hides if trash else None,
        )
        self.future.add_done_callback(lambda future: GLib.idle_add(self._done, future))

    def _progress(self, done, total):
        if not self.closed:
            value = text("export.zip.progress", done=done, total=total)
            self.progress_label.set_label(value)
            self.set_title(value)
        return GLib.SOURCE_REMOVE

    def _done(self, future):
        self.busy = False
        self.active_directories = frozenset()
        self.window._refresh_export()
        if self.closed:
            return GLib.SOURCE_REMOVE
        try:
            self.output = future.result()
        except CancelledError:
            self.force_close()
        except Exception as error:
            self._failed(error)
        else:
            self.success_toast = Adw.Toast(
                title=GLib.markup_escape_text(plural("export.zip.done", self._target_count)),
                button_label=text("common.show"),
                priority=Adw.ToastPriority.HIGH,
            )
            self.success_toast.connect("button-clicked", self._show_output)
            self.window.toasts.add_toast(self.success_toast)
            self.force_close()
        return GLib.SOURCE_REMOVE

    def _failed(self, error):
        self.busy = False
        self.active_directories = frozenset()
        self.set_title(text("export.zip.all"))
        self.stack.set_visible_child_name("select")
        self.update_snapshot(self.window.snapshot)
        self.window._refresh_export()
        self.window.toast(_("Could not export: {error}").format(error=str(error)))

    def _show_output(self, *_):
        launcher = Gtk.FileLauncher(file=Gio.File.new_for_path(str(self.output)))
        launcher.open_containing_folder(self.window, None, self._shown)

    def _shown(self, launcher, result):
        try:
            launcher.open_containing_folder_finish(result)
        except GLib.Error as error:
            if self.window.get_application() is not None:
                self.window.toast(str(error))

    def cancel(self, *_):
        self.cancelled.set()
        if self.folder_cancel:
            self.folder_cancel.cancel()
        self.force_close()

    def _closed(self, *_):
        self.closed = True
        self.busy = False
        self.active_directories = frozenset()
        self.cancelled.set()
        if self.folder_cancel:
            self.folder_cancel.cancel()
        self.executor.shutdown(wait=False, cancel_futures=True)
        if self.window.get_application() is not None:
            self.window._refresh_export()
