# SPDX-License-Identifier: GPL-3.0-or-later
"""The shared Export dialog (HARMONY §9, L-11): two primary choices, More formats, native folder selection."""

import errno
import os
import threading
from concurrent.futures import CancelledError, ThreadPoolExecutor
from pathlib import Path

from gi.repository import Adw, Gio, GLib, Gtk, Pango

from . import ux_formats as fmt
from .export_model import available_formats, encode_choice
from .exporting import export_meeting
from .i18n import gettext as _
from .i18n import join_list
from .library_window import label, vertical
from .strings import text

# PARITY §2 (L-11, D99): catalog label and caption for every single-meeting format.
FORMAT_TEXT = {
    "cassini": ("export.cassiniFile.label", "export.cassiniFile.description"),
    "original": ("export.originalAudio.label", "export.originalAudio.description"),
    "markdown": ("export.markdown.label", "export.markdown.description"),
    "txt": ("export.plainText.label", "export.plainText.description"),
    "srt": ("export.srt.label", "export.srt.description"),
    "vtt": ("export.vtt.label", "export.vtt.description"),
}
MORE_FORMATS = ("txt", "srt", "vtt")


def audio_size_label(meeting, kind):
    """`about {n} MB` for a Cassini file; the exact size for the unchanged original audio."""
    if kind == "original":
        size = meeting.audio_size_bytes
        return fmt.size(size) if type(size) is int and size >= 0 else None
    if meeting.audio_file:
        estimate = meeting.audio_size_bytes
    else:
        choice = encode_choice(
            [
                dict(codec="pcm", sampleRate=t.sample_rate, empty=t.frames < t.sample_rate, speechBitrateBps=None)
                for t in meeting.tracks
            ]
        )
        estimate = (meeting.duration_ms or 0) * choice["bitrateBps"] // 8000
    return fmt.size(estimate, approx=True) if type(estimate) is int and estimate >= 0 else None


def speaker_names_markup(meeting):
    names = [GLib.markup_escape_text(s.name) for s in meeting.speakers if s.provenance == "recognised"]
    if not names:
        return text("export.speakerNames.body.none")
    names = [f"<b>{name}</b>" for name in names]
    if len(names) == 1:
        return text("export.speakerNames.body", name=names[0])
    return text("export.speakerNames.body.several", names=join_list(names))


class ExportDialog(Adw.Dialog):
    def __init__(self, window, meeting):
        super().__init__(title=text("export.title", title=meeting.title), content_width=540)
        self.add_css_class("export-dialog")
        self.set_can_close(False)
        self.connect("close-attempt", self.cancel)
        self.window, self.meeting = window, meeting
        self.settings = window.get_application().recording_settings
        uri = self.settings.get_string("export-folder-uri")
        self.destination = Gio.File.new_for_uri(uri) if uri else None
        self.suggested_folder = Gio.File.new_for_path(
            GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOCUMENTS) or str(Path.home() / "Documents")
        )
        self.choosing = False
        self.folder_cancel = None
        self.success_toast = None
        self.cancelled = threading.Event()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cassini-export")
        self.closed = False
        self.busy = False
        self.output = None
        outer = vertical(22, margin_start=28, margin_end=28, margin_top=26, margin_bottom=26)
        heading = vertical(10)
        self.heading = label(self.get_title(), "export-heading", wrap=True, justify=Gtk.Justification.CENTER)
        heading.append(self.heading)
        heading.append(label(text("export.subtitle"), "dim-label", wrap=True, justify=Gtk.Justification.CENTER))
        outer.append(heading)
        choices = vertical(css_classes=["export-choices"], accessible_role=Gtk.AccessibleRole.GROUP)
        choices.update_property([Gtk.AccessibleProperty.LABEL], [text("export.formatGroup.a11y")])
        self.cassini = Gtk.CheckButton()  # The audio choice: Cassini file, or Original audio (D99 §2).
        self.markdown = Gtk.CheckButton()
        self.buttons = {"audio": self.cassini, "markdown": self.markdown}
        for kind in MORE_FORMATS:
            self.buttons[kind] = Gtk.CheckButton()
        for button in self.buttons.values():
            if button is not self.cassini:
                button.set_group(self.cassini)
        self.audio_kind = None
        self.choice_rows = []
        self.rows = {}
        self.audio_name, self.audio_description, self.audio_size = self._choice_row(choices, "audio")
        self._choice_row(choices, "markdown")
        self.more = Gtk.Expander(label=text("export.moreFormats"), css_classes=["export-more"])
        more_rows = vertical(css_classes=["export-more-rows"])
        for kind in MORE_FORMATS:
            self._choice_row(more_rows, kind)
        self.more.set_child(more_rows)
        choices.append(self.more)
        self.cassini.set_active(True)
        self.audio_only_note = label(
            text("export.audioOnly.note"), "small", "dim-label", "export-audio-only", wrap=True, xalign=0
        )
        self._apply_formats(meeting)
        outer.append(choices)
        outer.append(self.audio_only_note)
        names = vertical(8)
        names.append(label(text("export.speakerNames.heading"), "small", "heading", "dim-label", xalign=0))
        self.names = label("", wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR, xalign=0)
        self.names.set_markup(speaker_names_markup(meeting))
        names.append(self.names)
        outer.append(names)
        destination = Gtk.Box(spacing=12, css_classes=["export-destination"])
        destination.append(Gtk.Image(icon_name="folder-symbolic", accessible_role=Gtk.AccessibleRole.PRESENTATION))
        self.folder = label("", hexpand=True, xalign=0, wrap=True)
        destination.append(self.folder)
        self.choose = Gtk.Button(label=text("common.choose"))
        self.choose.connect("clicked", self._choose)
        destination.append(self.choose)
        outer.append(destination)
        buttons = Gtk.Box(spacing=10, homogeneous=True)
        cancel = Gtk.Button(label=text("common.cancel"))
        cancel.connect("clicked", self.cancel)
        buttons.append(cancel)
        self.export_button = Gtk.Button(label=text("export.exportButton"), css_classes=["suggested-action"])
        self.export_button.connect("clicked", self._export)
        buttons.append(self.export_button)
        outer.append(buttons)
        self.set_child(
            Gtk.ScrolledWindow(
                child=outer,
                hscrollbar_policy=Gtk.PolicyType.NEVER,
                propagate_natural_height=True,
                max_content_height=660,
            )
        )
        self.connect("closed", self._closed)
        self._update_folder()

    def _choice_row(self, parent, kind):
        button = self.buttons[kind]
        row = Gtk.Box(spacing=14, css_classes=["export-choice"])
        row.append(button)
        body = vertical(4, hexpand=True)
        name_key, description_key = FORMAT_TEXT["cassini" if kind == "audio" else kind]
        name = label(text(name_key), "heading", xalign=0)
        description = label(text(description_key), "small", "dim-label", wrap=True, xalign=0, max_width_chars=34)
        body.append(name)
        body.append(description)
        row.append(body)
        size = label("", "small", "dim-label", valign=Gtk.Align.CENTER, visible=False)
        row.append(size)
        parent.append(row)
        self.choice_rows.append(row)
        self.rows[kind] = row
        button.update_property([Gtk.AccessibleProperty.LABEL], [text(name_key)])
        button.connect(
            "toggled",
            lambda active, row=row: (row.add_css_class if active.get_active() else row.remove_css_class)("selected"),
        )
        click = Gtk.GestureClick()
        click.connect("released", lambda _gesture, _n, _x, _y, button=button: button.set_active(True))
        row.add_controller(click)
        return name, description, size

    def _apply_formats(self, meeting):
        """L-11: text formats only for a completed transcript; hidden, not greyed out, otherwise."""
        formats = available_formats(meeting)
        audio = formats[0]
        if audio != self.audio_kind:
            self.audio_kind = audio
            name_key, description_key = FORMAT_TEXT[audio]
            self.audio_name.set_label(text(name_key))
            self.audio_description.set_label(text(description_key))
            self.cassini.update_property([Gtk.AccessibleProperty.LABEL], [text(name_key)])
        size = audio_size_label(meeting, audio)
        self.audio_size.set_label(size or "")
        self.audio_size.set_visible(bool(size))
        transcribed = "markdown" in formats
        if not transcribed and not self.cassini.get_active():
            self.cassini.set_active(True)
        self.rows["markdown"].set_visible(transcribed)
        self.more.set_visible(transcribed)
        self.audio_only_note.set_visible(not transcribed)

    def selected_format(self):
        if self.cassini.get_active():
            return self.audio_kind
        return next(kind for kind, button in self.buttons.items() if kind != "audio" and button.get_active())

    def _update_folder(self):
        self.folder_name = (self.destination or self.suggested_folder).get_basename() or ""
        self.folder.set_markup(text("export.saveTo", folder="<b>" + GLib.markup_escape_text(self.folder_name) + "</b>"))

    def _controls(self, enabled):
        for control in (self.export_button, self.choose, self.more, *self.buttons.values()):
            control.set_sensitive(enabled)

    def _choose(self, *_, export_after=False):
        if self.busy or self.choosing or self.closed:
            return
        self.choosing = True
        self._controls(False)
        self.folder_cancel = Gio.Cancellable()
        dialog = Gtk.FileDialog(title=text("common.choose"), initial_folder=self.destination or self.suggested_folder)
        dialog.select_folder(
            self.window,
            self.folder_cancel,
            lambda chooser, result: self._chosen(chooser, result, export_after),
        )

    def _chosen(self, dialog, result, export_after=False):
        self.choosing = False
        self.folder_cancel = None
        if not self.closed:
            self._controls(True)
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
        self.destination = folder  # Keep the portal-returned URI, not a guessed host path.
        self.settings.set_string("export-folder-uri", folder.get_uri())
        self._update_folder()
        if export_after:
            self._export()

    def update(self, meeting):
        if self.busy or self.closed or meeting.key != self.meeting.key:
            return
        self.meeting = meeting
        self.set_title(text("export.title", title=meeting.title))
        self.heading.set_label(self.get_title())
        self.names.set_markup(speaker_names_markup(meeting))
        self._apply_formats(meeting)

    def _export(self, *_):
        if self.busy or self.choosing or self.closed:
            return
        if self.destination is None:
            self._choose(export_after=True)
            return
        page = self.window.meeting_page
        if page and page.meeting.key == self.meeting.key:
            self.update(page.meeting)
        elif self.window.snapshot:
            latest = next((m for m in self.window.snapshot.meetings if m.key == self.meeting.key), None)
            if latest:
                self.update(latest)
        self.busy = True
        self._controls(False)
        future = self.future = self.executor.submit(
            export_meeting,
            self.meeting,
            self.destination.get_path(),
            self.selected_format(),
            tz=self.window.tz,
            cancel=self.cancelled,
        )
        future.add_done_callback(lambda result: GLib.idle_add(self._done, result))

    def _done(self, future):
        if self.closed:
            return False
        try:
            self.output = future.result()
        except CancelledError:
            self.force_close()
        except Exception as error:
            self._failed(error)
        else:
            self.success_toast = Adw.Toast(
                title=GLib.markup_escape_text(
                    text("meeting.exported", title=self.meeting.title, folder=self.folder_name)
                ),
                button_label=text("common.show"),
                priority=Adw.ToastPriority.HIGH,
            )
            self.success_toast.connect("button-clicked", self._show_output)
            self.window.toasts.add_toast(self.success_toast)
            self.force_close()
        return False

    def _failed(self, error):
        self.busy = False
        self.window.toast(_("Could not export: {error}").format(error=str(error)))
        self._controls(True)

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
        self.cancelled.set()
        if self.folder_cancel:
            self.folder_cancel.cancel()
        self.executor.shutdown(wait=False, cancel_futures=True)
