# SPDX-License-Identifier: GPL-3.0-or-later
"""The Library shell. Filesystem reads and transcript decoding stay in the monitor worker."""

import contextlib
import os
import time
from dataclasses import replace
from functools import partial
from pathlib import Path

from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango

from . import import_feedback
from . import ux_formats as fmt
from .capture_activity import BrowserActivity, application_activity
from .capture_guard import recorder_stopping
from .folder_player import FolderPlayer
from .i18n import gettext as _
from .library import LibrarySnapshot, _sort, meeting_id
from .library_monitor import LibraryMonitor
from .listen import ListenController
from .meeting_page import MeetingPage
from .meeting_state import import_entry_badge
from .player import Player
from .spectro_views import BrokerFeed, NativeFeed, WindowSpectrograms
from .spectrogram_strip import SpectrogramStrip
from .status_badge import BadgeGlyph, StatusBadge
from .strings import text
from .tokens import speaker_slot


def label(value, *classes, **kwargs):
    return Gtk.Label(label=value, css_classes=list(classes), **kwargs)


def vertical(spacing=0, **kwargs):
    return Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=spacing, **kwargs)


def star_glyph():
    """The subtle star on a starred Library or search row (L-34, D99)."""
    glyph = Gtk.Image(
        icon_name="starred-symbolic",
        pixel_size=12,
        valign=Gtk.Align.CENTER,
        css_classes=["row-star", "dim-label"],
        accessible_role=Gtk.AccessibleRole.IMG,
    )
    glyph.update_property([Gtk.AccessibleProperty.LABEL], [text("library.row.starred.a11y")])
    return glyph


class LibraryWindow(Adw.ApplicationWindow):
    def __init__(self, application):
        super().__init__(application=application, title=text("app.name"), default_width=1120, default_height=720)
        self.add_css_class("library-window")
        self._pending = True
        self.selected = None
        self._preview_key = None
        self._updating_rows_selection = False
        self._loaded_key = None
        self._loaded_directory = None
        self.snapshot = None
        self._source_snapshot = None
        self._observed_phases = None
        self.meeting_page = None
        self.search_page = None
        self.recording_page = None
        self._external_meeting = False
        self.export_dialog = None
        self.zip_export_dialog = None
        self.recording_details_dialog = None
        self.rename_dialog = None
        self._pending_selection = None
        # L-57: meetings newly copied into the Library in this session that have
        # not yet shown their one-time "Imported from …" notice.
        self._fresh_imports = set()
        self._opening_files = 0
        self._import_cancels = set()
        self._import_dialogs = set()
        self._import_executor = None
        self._active_imports = {}
        self._failed_imports = {}
        self._import_row_widgets = []
        self._rendered_imports = ()
        self._drop_state = ("rest", None)
        self._drop_error_timer = 0
        self._drag_active = False
        self._drag_generation = 0
        self._drag_cancellable = None
        self._drag_dir_cache = {}
        self._drag_inflight = set()
        self._active_drag_files = ()
        self._drop_validation_gen = 0
        self._drop_validation_cancellable = None
        self._closing_search = False
        self._stars = None
        self.library_filter = "all"
        self._sidebar_scroll_source = 0
        self._list_signature = None
        self._row_by_key = {}
        self._rows_layout = None
        self._group_layout_key = None
        self._rows_tz = None
        self._preview_signature_value = None
        self._live_badges = {}
        self._capture_headers = []
        self.spectrograms = WindowSpectrograms(self)
        self._spectro_style = Adw.StyleManager.get_default()
        self._spectro_settings = Gtk.Settings.get_default()
        self._spectro_handlers = [
            (self._spectro_style, self._spectro_style.connect("notify::dark", self._spectrogram_style)),
            (
                self._spectro_settings,
                self._spectro_settings.connect("notify::gtk-enable-animations", self._spectrogram_style),
            ),
        ]
        self._spectrogram_style()
        self._focused_editors = set()
        self._activity_source = None
        self._activity_timer = 0
        # These are hidden harness inputs, never separate production fixtures or fake widget data.
        self._fixed_now = (
            int(os.environ["CASSINI_SCREENSHOT_NOW"])
            if os.environ.get("CASSINI_SCREENSHOT_ISOLATED") == "1" and os.environ.get("CASSINI_SCREENSHOT_NOW")
            else None
        )
        self.tz = fmt.local_timezone(os.environ.get("TZ") or GLib.TimeZone.new_local().get_identifier())
        self._rate = 1.0
        self.player = Player()
        self._playback_failed = False
        # D70: listening follows whichever Player the window holds; every path
        # that stops or replaces playback stops listening too.
        self.listen = ListenController(lambda: self.player)
        self._connect_player()
        self._fixture_activity = {}
        if os.environ.get("CASSINI_SCREENSHOT_ISOLATED") == "1" and os.environ.get("CASSINI_SCREENSHOT_ACTIVITY"):
            import json
            from pathlib import Path

            self._fixture_activity = json.loads(Path(os.environ["CASSINI_SCREENSHOT_ACTIVITY"]).read_text())
        self.root_box = Gtk.Stack(hhomogeneous=False, vhomogeneous=False)
        self.navigation = Adw.NavigationView()
        self.navigation.add(Adw.NavigationPage.new(self.root_box, text("app.name")))
        self.navigation.connect("popped", self._page_popped)
        self.toasts = Adw.ToastOverlay(child=self.navigation)
        self.update_banner = Adw.Banner(
            title=text("update.restartNeeded"),
            button_label=text("update.restart"),
            action_name="app.restart",
            revealed=False,
        )
        shell = Adw.ToolbarView(content=self.toasts)
        shell.add_top_bar(self.update_banner)
        from .parakeet_suggestion_widget import ParakeetSuggestion

        self.parakeet_suggestion = ParakeetSuggestion(application)
        shell.add_top_bar(self.parakeet_suggestion)
        self.set_content(shell)
        self.update_build_banner()
        self._build_empty()
        self._build_library()
        self.loading = Gtk.Spinner(spinning=True, halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        self.root_box.add_named(self.loading, "loading")
        self.root_box.set_visible_child_name("loading")
        self._activity = dict(self._fixture_activity)
        self.monitor = LibraryMonitor(self._library_scanned, capture_activity=lambda: self._activity)
        self._ensure_star_store()
        # Native clocks, meters and date grouping also need this tick when the
        # optional host browser bridge is unavailable or still being probed.
        self._activity_timer = GLib.timeout_add_seconds(1, self._activity_tick)
        self.companion_available_changed()
        self.monitor.refresh()
        self.connect("close-request", self._close)
        self.connect(
            "notify::visible",
            lambda *_: (
                self.get_application().meeting_trash.window_hidden(self)
                if not self.get_visible() and self.get_application()
                else None
            ),
        )
        for name, callback in (
            ("back-library", self._back_action),
            ("search", self.toggle_search),
            ("find-in-meeting", self._find_in_meeting),
            ("export", self.show_export),
            ("export-zip", self.show_zip_export),
            ("rename", self.show_rename),
            ("name-speakers", lambda *_: self.meeting_page.name_speakers() if self.meeting_page else None),
            ("move-to-trash", self.move_to_trash),
            ("toggle-star", self.toggle_star),
            ("recording-details", self.show_recording_details),
            ("undo-speaker-merge", lambda *_: self.meeting_page.undo_speaker_merge() if self.meeting_page else None),
            ("play-pause", self._play_selected),
            ("skip-back", lambda *_: self._skip_selected(-10000)),
            ("skip-forward", lambda *_: self._skip_selected(10000)),
            ("rate-up", lambda *_: self._step_rate(1)),
            ("rate-down", lambda *_: self._step_rate(-1)),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda _action, _param, call=callback: call())
            self.add_action(action)
        rate = Gio.SimpleAction.new_stateful("rate", GLib.VariantType.new("s"), GLib.Variant("s", "1"))
        rate.connect("change-state", self._change_rate)
        self.add_action(rate)
        find_shortcut = Gtk.ShortcutController(
            scope=Gtk.ShortcutScope.MANAGED,
            propagation_phase=Gtk.PropagationPhase.CAPTURE,
        )
        find_shortcut.add_shortcut(
            Gtk.Shortcut.new(
                Gtk.KeyvalTrigger.new(Gdk.KEY_f, Gdk.ModifierType.CONTROL_MASK),
                Gtk.CallbackAction.new(lambda *_: self._find_in_meeting()),
            )
        )
        self.add_controller(find_shortcut)
        target = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        target.set_preload(True)
        target.connect("enter", self._drag_enter)
        target.connect("motion", self._drag_motion)
        target.connect("leave", self._drag_leave)
        target.connect("notify::value", self._drag_value_changed)
        target.connect("drop", self._drop_files)
        self.add_controller(target)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._trash_key)
        self.add_controller(keys)
        self._refresh_export()

    def toast(self, message):
        self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(message)))

    def show_recording_details(self):
        from .capture_details_dialog import RecordingDetailsDialog

        page = self.recording_page or self.meeting_page
        meeting = page.meeting if page else self.selected
        if meeting is not None:
            if not self.recording_details_dialog or self.recording_details_dialog.closed:
                self.recording_details_dialog = RecordingDetailsDialog(self, meeting)
            self.recording_details_dialog.present(self)

    @staticmethod
    def _classify_drop_file_sync(file, dir_cache=None):
        """Classify a dropped Gio.File without touching the filesystem on the GTK main thread."""
        raw = file.get_path()
        if raw:
            path = Path(raw)
            name = path.name
            suffix = path.suffix.lower()
            if import_feedback.droppable_name(name) or LibraryWindow._is_folder_open_path(path):
                return ("valid", None)
            if suffix:
                return ("invalid", name or raw)
            if dir_cache is not None and raw in dir_cache:
                return ("valid", None) if dir_cache[raw] else ("invalid", name or raw)
            return ("unknown", raw, name or raw, file)
        basename = file.get_basename()
        if basename:
            suffix = Path(basename).suffix.lower()
            if import_feedback.droppable_name(basename) or basename == "session.json":
                return ("valid", None)
            if suffix:
                return ("invalid", basename)
            uri = file.get_uri()
            if dir_cache is not None and uri in dir_cache:
                return ("valid", None) if dir_cache[uri] else ("invalid", basename)
            return ("unknown", uri, basename, file)
        return ("valid", None)

    def _probe_is_dir_async(self, gfile, cancellable, callback):
        def on_info(source, result):
            try:
                info = source.query_info_finish(result)
            except GLib.Error as error:
                if error.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED):
                    return
                callback(False)
                return
            callback(info.get_file_type() == Gio.FileType.DIRECTORY)

        gfile.query_info_async(
            "standard::type",
            Gio.FileQueryInfoFlags.NONE,
            GLib.PRIORITY_DEFAULT,
            cancellable,
            on_info,
        )

    def _reset_drag_session(self):
        self._drag_generation = getattr(self, "_drag_generation", 0) + 1
        cancellable = getattr(self, "_drag_cancellable", None)
        if cancellable is not None:
            cancellable.cancel()
            self._drag_cancellable = None
        if hasattr(self, "_drag_dir_cache"):
            self._drag_dir_cache.clear()
        else:
            self._drag_dir_cache = {}
        if hasattr(self, "_drag_inflight"):
            self._drag_inflight.clear()
        else:
            self._drag_inflight = set()
        self._active_drag_files = ()

    def _cancel_drop_validation(self):
        self._drop_validation_gen = getattr(self, "_drop_validation_gen", 0) + 1
        cancellable = getattr(self, "_drop_validation_cancellable", None)
        if cancellable is not None:
            cancellable.cancel()
            self._drop_validation_cancellable = None

    def _clear_sticky_drop_error(self):
        self._drop_error_timer = 0
        if self._drop_state[0] == "error":
            self._set_drop_state("rest")
        return GLib.SOURCE_REMOVE

    def _set_drop_state(self, state, bad_file=None, *, sticky_error=False):
        if self._drop_error_timer:
            GLib.source_remove(self._drop_error_timer)
            self._drop_error_timer = 0
        self._drop_state = (state, bad_file)
        if not hasattr(self, "drop_frame"):
            return
        (self.drop_frame.add_css_class if state == "enter" else self.drop_frame.remove_css_class)("drag-enter")
        (self.drop_frame.add_css_class if state in ("over", "error") else self.drop_frame.remove_css_class)("drag-over")
        (self.drop_footer.add_css_class if state == "enter" else self.drop_footer.remove_css_class)("drag-enter")
        self.drop_footer_label.set_label(text("import.drop.enter" if state == "enter" else "import.drop.rest"))
        if state == "over":
            self.drop_overlay.remove_css_class("error")
            self.drop_overlay_glyph.set_glyph_kind("incoming", size_px=28)
            self.drop_overlay_title.set_label(text("import.drop.over"))
            self.drop_overlay_detail.set_label(import_feedback.accepted_formats())
            self.drop_overlay.set_visible(True)
        elif state == "error":
            self.drop_overlay.add_css_class("error")
            self.drop_overlay_glyph.set_glyph_kind("alert", size_px=28)
            self.drop_overlay_title.set_label(text("import.drop.cantImport", file=bad_file or ""))
            self.drop_overlay_detail.set_label(import_feedback.cant_import_detail())
            self.drop_overlay.set_visible(True)
            if sticky_error:
                self._drop_error_timer = GLib.timeout_add(4000, self._clear_sticky_drop_error)
        else:
            self.drop_overlay.set_visible(False)

    def _point_in_drop_list(self, x, y):
        if not hasattr(self, "drop_frame") or not self.drop_frame.get_mapped():
            return False
        coords = self.translate_coordinates(self.drop_frame, x, y)
        if coords is None:
            return False
        lx, ly = coords
        return 0 <= lx <= self.drop_frame.get_width() and 0 <= ly <= self.drop_frame.get_height()

    def _evaluate_drag_files(self, files):
        files = tuple(files or ())
        self._active_drag_files = files
        unknowns = []
        for file in files:
            verdict = self._classify_drop_file_sync(file, self._drag_dir_cache)
            if verdict[0] == "invalid":
                return verdict[1]
            if verdict[0] == "unknown":
                unknowns.append(verdict[1:])
        if unknowns:
            if self._drag_cancellable is None:
                self._drag_cancellable = Gio.Cancellable()
            gen = self._drag_generation
            cancellable = self._drag_cancellable
            for key, _display, gfile in unknowns:
                if key in self._drag_inflight:
                    continue
                self._drag_inflight.add(key)

                def on_probed(is_dir, item_key=key, expected_gen=gen):
                    if expected_gen != self._drag_generation or getattr(
                        getattr(self, "monitor", None), "_closed", False
                    ):
                        return
                    self._drag_inflight.discard(item_key)
                    self._drag_dir_cache[item_key] = bool(is_dir)
                    if (
                        self._drag_active
                        and self._drop_state[0] in ("enter", "over")
                        and hasattr(self, "_last_drag_xy")
                        and self._point_in_drop_list(*self._last_drag_xy)
                    ):
                        bad_now = self._evaluate_drag_files(self._active_drag_files)
                        if bad_now is not None:
                            self._set_drop_state("error", bad_now)

                self._probe_is_dir_async(gfile, cancellable, on_probed)
        return None

    def _drag_bad_file(self, target):
        if target is None:
            return None
        value = target.get_value()
        if isinstance(value, Gdk.FileList):
            return self._evaluate_drag_files(value.get_files())
        return None

    def _drag_enter(self, target, x, y):
        if self._drag_active:
            self._reset_drag_session()
        self._drag_active = True
        return self._drag_motion(target, x, y)

    def _drag_motion(self, target, x, y):
        self._drag_active = True
        self._last_drag_xy = (x, y)
        bad = self._drag_bad_file(target)
        if self._point_in_drop_list(x, y):
            if bad is not None:
                self._set_drop_state("error", bad)
            else:
                self._set_drop_state("over")
        else:
            self._set_drop_state("enter")
        return Gdk.DragAction.COPY

    def _drag_value_changed(self, target, *_args):
        if not self._drag_active:
            return
        bad = self._drag_bad_file(target)
        if self._drop_state[0] in ("enter", "over") and hasattr(self, "_last_drag_xy"):
            x, y = self._last_drag_xy
            if self._point_in_drop_list(x, y) and bad is not None:
                self._set_drop_state("error", bad)

    def _drag_leave(self, *_args):
        self._drag_active = False
        self._reset_drag_session()
        if not self._drop_error_timer:
            self._set_drop_state("rest")

    def _show_rejected_drop(self, bad):
        if hasattr(self, "drop_frame") and self.root_box.get_visible_child_name() == "library":
            if self.search_page:
                self.back_to_library()
            self._set_drop_state("error", bad, sticky_error=True)
        else:
            self._set_drop_state("rest")
            self.toast(f"{text('import.drop.cantImport', file=bad)} {import_feedback.cant_import_detail()}")

    def _validate_dropped_files_async(self, chosen, cached):
        self._cancel_drop_validation()
        drop_gen = self._drop_validation_gen
        cancellable = Gio.Cancellable()
        self._drop_validation_cancellable = cancellable
        local_cache = dict(cached)
        pending = {}
        for file in chosen:
            verdict = self._classify_drop_file_sync(file, local_cache)
            if verdict[0] == "unknown":
                _, key, _display, gfile = verdict
                if key not in pending:
                    pending[key] = gfile
        remaining = set(pending)

        def finish_if_done():
            if drop_gen != self._drop_validation_gen or getattr(getattr(self, "monitor", None), "_closed", False):
                return
            for item in chosen:
                res = self._classify_drop_file_sync(item, local_cache)
                if res[0] == "invalid":
                    self._cancel_drop_validation()
                    self._show_rejected_drop(res[1])
                    return
            if not remaining:
                self._cancel_drop_validation()
                self.open_files(chosen)

        for key, gfile in pending.items():

            def on_probed(is_dir, item_key=key):
                if drop_gen != self._drop_validation_gen or getattr(getattr(self, "monitor", None), "_closed", False):
                    return
                local_cache[item_key] = bool(is_dir)
                remaining.discard(item_key)
                finish_if_done()

            self._probe_is_dir_async(gfile, cancellable, on_probed)

    def _drop_files(self, _target, files, _x, _y):
        cached = dict(self._drag_dir_cache)
        self._drag_active = False
        self._reset_drag_session()
        chosen = tuple(files.get_files()) if files else ()
        if not chosen:
            self._set_drop_state("rest")
            return False
        has_unknown = False
        for file in chosen:
            verdict = self._classify_drop_file_sync(file, cached)
            if verdict[0] == "invalid":
                self._cancel_drop_validation()
                self._show_rejected_drop(verdict[1])
                return False
            if verdict[0] == "unknown":
                has_unknown = True
        self._set_drop_state("rest")
        if has_unknown:
            self._validate_dropped_files_async(chosen, cached)
            return True
        self._cancel_drop_validation()
        self.open_files(chosen)
        return True

    def _refresh_export(self):
        from .zip_export import is_exportable

        app = self.get_application()
        trash = app.meeting_trash if app else None
        meeting = self.meeting_page.meeting if self.meeting_page else self.selected
        action = self.lookup_action("export")
        if action:
            action.set_enabled(
                bool(
                    meeting
                    and meeting.has_audio
                    and meeting.lifecycle == "saved"
                    and not self.recording_page
                    and not self.search_page
                )
            )
        zip_action = self.lookup_action("export-zip")
        if zip_action:
            zip_action.set_enabled(
                bool(
                    not self.recording_page
                    and not self.search_page
                    and not self._external_meeting
                    and self.snapshot is not None
                    and not self.snapshot.unavailable
                    and any(is_exportable(m, trash=trash) for m in self.snapshot.meetings)
                    and not (
                        self.zip_export_dialog and not self.zip_export_dialog.closed and self.zip_export_dialog.busy
                    )
                )
            )

        rename = self.lookup_action("rename")
        if rename:
            rename.set_enabled(self.rename_allowed(meeting))
        name_speakers = self.lookup_action("name-speakers")
        if name_speakers:
            name_speakers.set_enabled(
                bool(
                    self.meeting_page
                    and meeting
                    and meeting.speakers
                    and meeting.metadata_editable
                    and not self.recording_page
                    and not self.search_page
                )
            )
        star = self.lookup_action("toggle-star")
        if star:
            star.set_enabled(LibraryWindow.star_allowed(self, meeting))
        trash = self.lookup_action("move-to-trash")
        if trash:
            trash.set_enabled(self.get_application().meeting_trash.allowed(meeting, self) and not self._focused_editors)

    @property
    def play_button(self):
        return self.meeting_page.play if self.meeting_page is not None else None

    @property
    def stop_transcription_button(self):
        return self.meeting_page.stop_transcription_button if self.meeting_page is not None else None

    def is_playable(self, meeting):
        return bool(
            meeting
            and meeting.has_audio
            and not self._playback_failed
            and (meeting.audio_file is not None or FolderPlayer.can_play(meeting))
        )

    def _find_in_meeting(self):
        if (
            self.meeting_page is not None
            and self.meeting_page.find_row.get_visible()
            and not self.search_page
            and not self.recording_page
        ):
            return self.meeting_page.focus_find()
        if (
            not (self.recording_page and (self._external_meeting or self._text_focused()))
            and not self._external_meeting
            and self.root_box.get_visible_child_name() == "library"
        ):
            self._enter_search(focus=True)
            return True
        return False

    def rename_allowed(self, meeting):
        app = self.get_application()
        return bool(
            meeting
            and meeting.metadata_editable
            and meeting.lifecycle == "saved"
            and not self.recording_page
            and not self.search_page
            and not self._external_meeting
            and not app._quitting
            and not (app.library_relocator and app.library_relocator.running)
            and not app.meeting_trash.hides(meeting.directory)
            and not (self.snapshot and self.snapshot.unavailable)
        )

    @staticmethod
    def rename_item():
        item = Gio.MenuItem.new(text("meeting.renameEllipsis"), "win.rename")
        item.set_attribute_value("accel", GLib.Variant("s", text("common.shortcut.rename")))
        return item

    def show_rename(self):
        from .rename_dialog import RenameDialog

        meeting = self.meeting_page.meeting if self.meeting_page else self.selected
        if not self.rename_allowed(meeting) or self._text_focused():
            return
        if self.rename_dialog is None or self.rename_dialog.closed:
            self.rename_dialog = RenameDialog(self, meeting)
        self.rename_dialog.present(self)

    def _library_scanned(self, snapshot):
        if hasattr(self, "_stars"):
            LibraryWindow._ensure_star_store(self)
        self._source_snapshot = snapshot
        self.get_application().meeting_trash.scanned(snapshot, self.monitor.scan_started)
        self._updated(snapshot)

    def _library_loaded(self, meeting):
        # A direct Open result is a newer observation than the cached scan.
        # Promote it before opening its page so activity/trash reprojection
        # cannot restore older metadata while the next full scan is pending.
        source = self._source_snapshot or self.snapshot or LibrarySnapshot()
        meetings = {row.key: row for row in source.meetings}
        meetings[meeting.key] = meeting
        source = replace(
            source,
            meetings=tuple(sorted(meetings.values(), key=_sort)),
            directories=tuple(sorted(set(source.directories) | {meeting.directory})),
        )
        self._source_snapshot = source
        self._updated(source)

    def _ensure_star_store(self):
        """One per-device star store per Library root (L-34); follows a moved Library."""
        from .stars import StarStore

        monitor = getattr(self, "monitor", None)
        if monitor is None:
            return None
        root = monitor.scanner.root
        if self._stars is None or self._stars.root != root:
            inherit = frozenset()
            if self._stars is not None:
                inherit = self._stars.starred
                self._stars.close()
            self._stars = StarStore(
                root,
                dispatch=lambda callback: GLib.idle_add(callback),
                on_changed=lambda store: self._stars_changed(store),
                inherit=inherit,
            )
        return self._stars

    def star_allowed(self, meeting):
        return bool(
            meeting
            and getattr(self, "_stars", None) is not None
            and not self._external_meeting
            and not meeting.key.startswith("unreadable/")
        )

    def is_starred(self, meeting):
        stars = getattr(self, "_stars", None)
        return bool(meeting and stars is not None and stars.is_starred(meeting.key))

    def toggle_star(self, meeting=None):
        meeting = meeting or (self.meeting_page.meeting if self.meeting_page else self.selected)
        if not self.star_allowed(meeting):
            return None
        from .stars import star_key

        return self._stars.toggle(star_key(meeting))

    def _stars_changed(self, store):
        if store is not self._stars:
            return
        for row in self._row_by_key.values():
            meeting = getattr(row, "meeting", None)
            if meeting is not None:
                signature = self._row_signature(meeting)
                if signature != row._row_signature:
                    self._fill_row(row, meeting, signature)
        self._apply_library_filter()
        if self.meeting_page is not None:
            self.meeting_page.sync_star()
        if self.search_page is not None:
            self.search_page.stars_changed()

    def set_library_filter(self, value):
        """L-34 All / Starred at rest: single choice, remembered on this device."""
        value = "starred" if value == "starred" else "all"
        if value == self.library_filter:
            return
        self.library_filter = value
        settings = getattr(self.get_application(), "recording_settings", None)
        if settings is not None and settings.get_string("library-filter") != value:
            settings.set_string("library-filter", value)
        button = self.filter_starred if value == "starred" else self.filter_all
        if not button.get_active():
            button.set_active(True)
        selected = self.rows.get_selected_row()
        if value == "starred" and selected is not None and not self._row_matches_filter(selected):
            self.rows.select_row(None)
        self._apply_library_filter()

    def _row_matches_filter(self, row):
        if self.library_filter != "starred":
            return True
        meeting = getattr(row, "meeting", None)
        return meeting is None or self.is_starred(meeting)

    def _row_visible(self, row):
        if self.library_filter != "starred":
            return True
        keys = getattr(row, "_group_keys", None)
        if keys is not None:
            return any((other := self._row_by_key.get(key)) is not None and self._row_visible(other) for key in keys)
        meeting = getattr(row, "meeting", None)
        if meeting is None:
            return True
        # The open meeting stays put when it is unstarred, until selection moves.
        return self.is_starred(meeting) or (self.selected is not None and self.selected.key == meeting.key)

    def _apply_library_filter(self):
        if not hasattr(self, "starred_empty"):
            return
        self.rows.invalidate_filter()
        snapshot = self.snapshot
        starred = self.library_filter == "starred"
        nothing = bool(
            starred
            and snapshot is not None
            and not snapshot.unavailable
            and not any(self.is_starred(m) for m in snapshot.meetings)
        )
        self.starred_empty.set_visible(nothing)

    def move_to_trash(self):
        meeting = self.meeting_page.meeting if self.meeting_page else self.selected
        self.get_application().meeting_trash.request(meeting, self)

    def trash_error_line(self, meeting):
        from .inline_line import InlineLine

        failure = self.get_application().meeting_trash.errors.get(meeting.directory)
        if failure is None:
            return None
        if failure == "unsupported":

            def show():
                launcher = Gtk.FileLauncher(file=Gio.File.new_for_path(str(meeting.directory)))
                launcher.open_containing_folder(self, None, None, None)

            return InlineLine("meeting.trashUnsupported", action="meeting.showInFiles", callback=show)
        return InlineLine(
            "meeting.trashFailed",
            action="common.tryAgain",
            callback=lambda: self.get_application().meeting_trash.request(meeting, self),
        )

    def publication_line(self, meeting):
        from .inline_line import InlineLine

        notice = meeting.publication_notice
        if not notice:
            return None
        if notice == "library.publishedMissing":
            from .publication_state import session_root_path

            def show():
                root = session_root_path(meeting.directory) or meeting.directory.parent
                Gtk.FileLauncher(file=Gio.File.new_for_path(str(root))).launch(self, None, None, None)

            return InlineLine(notice, info=True, action="meeting.showInFiles", callback=show)
        retry = notice in ("library.publishFailed", "library.publishDiskLow")
        recovery = getattr(self.get_application(), "library_recovery", None)
        return InlineLine(
            notice,
            action="common.tryAgain" if retry else None,
            callback=(lambda: recovery.retry(meeting.directory)) if retry and recovery else None,
        )

    def update_build_banner(self):
        app = self.get_application()
        refresh = app.build_refresh
        self.update_banner.set_revealed(bool(refresh and refresh.pending))
        app.lookup_action("restart").set_enabled(bool(refresh and refresh.can_restart))

    def _text_focused(self):
        widget = self.get_focus()
        while widget:
            if isinstance(widget, (Gtk.Editable, Gtk.TextView)):
                return True
            widget = widget.get_parent()
        return bool(self._focused_editors)

    def _trash_key(self, _keys, key, _code, modifiers):
        if self._text_focused() or self.search_page or self.recording_page or self.get_visible_dialog():
            return False
        focus = self.get_focus()
        in_list = bool(focus and (focus is self.rows or focus.is_ancestor(self.rows)))
        in_meeting = bool(
            self.meeting_page and focus and (focus is self.meeting_page or focus.is_ancestor(self.meeting_page))
        )
        blocked = (
            Gdk.ModifierType.CONTROL_MASK
            | Gdk.ModifierType.ALT_MASK
            | Gdk.ModifierType.SHIFT_MASK
            | Gdk.ModifierType.SUPER_MASK
        )
        if (
            key == Gdk.KEY_Delete
            and not modifiers & blocked
            and (in_meeting or in_list)
            and self.lookup_action("move-to-trash").get_enabled()
        ):
            self.move_to_trash()
            return True
        if in_list and (key == Gdk.KEY_Menu or key == Gdk.KEY_F10 and modifiers & Gdk.ModifierType.SHIFT_MASK):
            row = self.rows.get_selected_row()
            if row and hasattr(row, "meeting"):
                self._row_menu(row, 12, row.get_height() // 2)
                return True
        return False

    def _row_menu(self, row, x, y):
        self.rows.select_row(row)
        menu = Gio.Menu()
        menu.append_item(self.rename_item())
        menu.append(text("common.exportEllipsis"), "win.export")
        if self.star_allowed(row.meeting):
            star = Gio.Menu()
            star.append(text("meeting.unstar" if self.is_starred(row.meeting) else "meeting.star"), "win.toggle-star")
            menu.append_section(None, star)
        if self.get_application().meeting_trash.allowed(row.meeting, self) or (
            row.meeting.published_file is not None and not row.meeting.metadata_editable
        ):
            section = Gio.Menu()
            from .meeting_trash import menu_item

            section.append_item(menu_item())
            menu.append_section(None, section)
        popover = Gtk.PopoverMenu.new_from_model(menu)
        popover.set_parent(row)
        rectangle = Gdk.Rectangle()
        rectangle.x, rectangle.y, rectangle.width, rectangle.height = int(x), int(y), 1, 1
        popover.set_pointing_to(rectangle)

        def unparent(menu):
            if menu.get_parent() is not None:
                menu.unparent()
            return GLib.SOURCE_REMOVE

        # GtkModelButton closes its popover before activating the item. Keep
        # the window action group reachable until that activation has finished.
        popover.connect("closed", lambda menu: GLib.idle_add(unparent, menu))
        popover.popup()
        return popover

    def show_export(self):
        from .export_dialog import ExportDialog

        meeting = self.meeting_page.meeting if self.meeting_page else self.selected
        if (
            meeting is not None
            and meeting.has_audio
            and meeting.lifecycle == "saved"
            and not self.recording_page
            and not self.search_page
        ):
            if not self.export_dialog or self.export_dialog.closed:
                try:
                    self.export_dialog = ExportDialog(self, meeting)
                except OSError as error:
                    self.toast(_("Could not export: {error}").format(error=str(error)))
                    return
            self.export_dialog.present(self)

    def show_zip_export(self):
        from .zip_export import exportable_meetings
        from .zip_export_dialog import ZipExportDialog

        app = self.get_application()
        if (
            self.recording_page
            or self.search_page
            or self._external_meeting
            or not exportable_meetings(self.snapshot, trash=app.meeting_trash if app else None)
        ):
            return
        if not self.zip_export_dialog or self.zip_export_dialog.closed:
            try:
                self.zip_export_dialog = ZipExportDialog(self)
            except OSError as error:
                self.toast(_("Could not export: {error}").format(error=str(error)))
                return
        self.zip_export_dialog.present(self)

    def protect_text_input(self, widget):
        controller = Gtk.EventControllerFocus()
        controller.connect("enter", lambda *_: self._editor_focus(widget, True))
        controller.connect("leave", lambda *_: self._editor_focus(widget, False))
        widget.add_controller(controller)

    def _editor_focus(self, widget, focused):
        if focused:
            self._focused_editors.add(id(widget))
        else:
            self._focused_editors.discard(id(widget))
        for name in ("play-pause", "skip-back", "skip-forward"):
            action = self.lookup_action(name)
            if action:
                action.set_enabled(not self._focused_editors)
        self._refresh_export()
        if self.meeting_page:
            self.meeting_page.listening_keys.refresh()

    @property
    def now_ms(self):
        return self._fixed_now if self._fixed_now is not None else int(time.time() * 1000)

    def _record(self, big=False):
        button = Gtk.Button(
            halign=Gtk.Align.CENTER if big else Gtk.Align.FILL,
            css_classes=["cassini-record"] + (["big-record"] if big else []),
            action_name="app.record",
        )
        content = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        content.append(
            Gtk.Box(
                width_request=9,
                height_request=9,
                valign=Gtk.Align.CENTER,
                css_classes=["record-button-dot"],
                accessible_role=Gtk.AccessibleRole.PRESENTATION,
            )
        )
        content.append(Gtk.Label(label=text("common.record")))
        button.set_child(content)
        button.update_property([Gtk.AccessibleProperty.LABEL], [text("common.record")])
        return button

    def import_button(self):
        button = Gtk.Button(
            action_name="app.open",
            tooltip_text=text("import.action"),
            valign=Gtk.Align.CENTER,
            css_classes=["flat", "import-audio-btn"],
        )
        button.set_child(BadgeGlyph("incoming", size_px=16))
        button.update_property([Gtk.AccessibleProperty.LABEL], [text("import.action")])
        return button

    def _primary_menu_button(self, *, flat=False):
        menu = Gio.Menu()
        top = Gio.Menu()
        top.append(text("import.action"), "app.open")
        top.append(text("export.zip.all"), "win.export-zip")
        menu.append_section(None, top)
        bottom = Gio.Menu()
        bottom.append(text("settings.title"), "app.preferences")
        bottom.append(_("_Keyboard Shortcuts"), "app.shortcuts")
        bottom.append(_("_About Cassini").replace("Cassini", text("app.name")), "app.about")
        menu.append_section(None, bottom)
        button = Gtk.MenuButton(
            icon_name="open-menu-symbolic",
            menu_model=menu,
            tooltip_text=text("common.mainMenu"),
            primary=True,
            css_classes=["flat"] if flat else [],
        )
        button.update_property([Gtk.AccessibleProperty.LABEL], [text("common.mainMenu")])
        return button

    def _header(self, *, record=False, close=True):
        header = Adw.HeaderBar(
            show_start_title_buttons=False,
            show_end_title_buttons=close,
            decoration_layout=":close",
            css_classes=["library-header"],
        )
        header.set_title_widget(label(text("app.name"), "heading"))
        if record:
            header.pack_start(self.capture_controls())
        if close:
            header.pack_end(self._primary_menu_button())
            if not record:
                header.pack_end(Gtk.Button(label=text("common.exportEllipsis"), action_name="win.export"))
        return header

    def _first_run_header(self):
        """L-27: primary menu and window close only; no header Record and no visible title."""
        header = Adw.HeaderBar(
            show_start_title_buttons=False,
            show_end_title_buttons=True,
            decoration_layout=":close",
            css_classes=["library-header", "first-run-header"],
        )
        header.set_title_widget(Gtk.Box())
        header.pack_end(self._primary_menu_button())
        return header

    def _build_empty(self):
        """L-27 first-run hero (HARMONY section 10, COMPONENTS.md section 8)."""
        from .brand_mark import HERO_MARK_HEIGHT_PX, BrandMark
        from .first_run_hero import HeroWave

        view = Adw.ToolbarView()
        view.add_top_bar(self._first_run_header())
        overlay = Gtk.Overlay()
        outer = vertical(vexpand=True)
        overlay.set_child(outer)
        # Only a hidden accessible "Library" heading: transparent, but still in the accessibility tree.
        heading = self.first_run_heading = label(
            text("library.title"),
            "first-run-heading",
            accessible_role=Gtk.AccessibleRole.HEADING,
            halign=Gtk.Align.START,
            valign=Gtk.Align.START,
            can_target=False,
        )
        heading.set_opacity(0.0)
        overlay.add_overlay(heading)
        center = vertical(valign=Gtk.Align.CENTER, vexpand=True, hexpand=True, css_classes=["first-run"])
        # A 720 px column on wide windows that shrinks with narrow ones: a clamp caps
        # the width without making it the window's minimum (the header must stay on screen).
        hero = vertical(hexpand=True, css_classes=["first-run-hero"])
        clamp = Adw.Clamp(maximum_size=720, tightening_threshold=720, hexpand=True, child=hero)
        mark = BrandMark(HERO_MARK_HEIGHT_PX)
        mark.set_halign(Gtk.Align.START)
        mark.add_css_class("hero-mark")
        hero.append(mark)
        # One sentence per line, left-aligned, 64 px bold platform font; the second line dimmed.
        for key, classes in (
            ("firstRun.hero.title", ["hero-title"]),
            ("firstRun.hero.subtitle", ["hero-title", "hero-subtitle"]),
        ):
            # No wrap (drawn): ellipsize rather than reflow if a translation outgrows the column.
            line = label(text(key), *classes, xalign=0, halign=Gtk.Align.START, ellipsize=Pango.EllipsizeMode.END)
            hero.append(line)
        self.hero_title, self.hero_subtitle = hero.get_last_child().get_prev_sibling(), hero.get_last_child()
        wave_row = Gtk.Box(spacing=16, halign=Gtk.Align.START, css_classes=["hero-wave-row"])
        record = self._record(big=True)
        record.add_css_class("hero-record")
        record.set_halign(Gtk.Align.START)
        record.set_valign(Gtk.Align.CENTER)
        wave_row.append(record)
        self.hero_wave = HeroWave()
        wave_row.append(self.hero_wave)
        hero.append(wave_row)
        body = label(text("firstRun.body"), "dim-label", "hero-body", xalign=0, halign=Gtk.Align.START, wrap=True)
        body.set_max_width_chars(60)
        hero.append(body)
        self.first_recording_lines = vertical(visible=False)
        hero.append(self.first_recording_lines)
        companion = self.first_companion = Gtk.Button(
            label=text("companion.addStep.title"),
            css_classes=["flat"],
            halign=Gtk.Align.START,
            visible=self.get_application().browser_companion_available,
        )
        companion.connect("clicked", lambda *_: self.get_application().on_companion_setup(parent=self))
        hero.append(companion)
        from .browser_status import BrowserStatus

        self.first_browser_status = BrowserStatus(self.get_application(), self)
        hero.append(self.first_browser_status)
        # Provisional privacy line (firstRun.footer until L-09 / D-938 closes), first run only (L-30).
        footer = Gtk.Box(spacing=8, halign=Gtk.Align.START, css_classes=["hero-privacy"])
        footer.append(
            Gtk.Image(
                icon_name="changes-prevent-symbolic", pixel_size=14, accessible_role=Gtk.AccessibleRole.PRESENTATION
            )
        )
        footer.append(label(text("firstRun.footer"), "small", "dim-label", xalign=0, wrap=True))
        self.privacy_footer = footer
        hero.append(footer)
        center.append(clamp)
        outer.append(center)
        view.set_content(overlay)
        self.root_box.add_named(view, "empty")
        self.offline = label(text("errors.libraryOffline.line"), wrap=True, vexpand=True)
        offline_view = Adw.ToolbarView(content=self.offline)
        offline_view.add_top_bar(self._header(record=True))
        self.root_box.add_named(offline_view, "offline")

    def _spectrogram_style(self, *_):
        dark = self._spectro_style.get_dark()
        self.spectrograms.configure(
            dark=dark, reduced_motion=not self._spectro_settings.get_property("gtk-enable-animations")
        )

    def bind_spectrogram(self, widget, meeting, *, silent=False, stopping=False):
        if meeting is None:
            self.spectrograms.remove(widget)
            return
        # Both paths come from controller/scanner ownership. Avoid another
        # realpath traversal on every level callback merely to draw decoration.
        native = self.get_application().comfort._native_for(meeting)
        events = native.session.manifest.get("events", ()) if native and native.session else meeting.capture_events
        if native is not None and hasattr(native, "spectrogram"):
            factory = partial(NativeFeed, native)
        else:
            from .browser_service import service_path

            path = self._activity_source.path if self._activity_source else None
            factory = partial(BrokerFeed, path or service_path(), meeting.directory)
        self.spectrograms.bind(
            widget,
            meeting.key,
            factory,
            events=events,
            silent=silent,
            active=meeting.lifecycle in ("recording", "saving"),
            stopping=stopping or meeting.lifecycle != "recording",
        )

    def capture_controls(self, meeting=None):
        stack = Gtk.Stack(hhomogeneous=False, vhomogeneous=False)
        stack.add_named(self._record(), "record")
        live = Gtk.Box(
            spacing=8, css_classes=["live-pill"], valign=Gtk.Align.CENTER, tooltip_text=text("recording.pill.noSleep")
        )
        live.append(
            Gtk.Box(
                width_request=8,
                height_request=8,
                valign=Gtk.Align.CENTER,
                css_classes=["live-dot"],
                accessible_role=Gtk.AccessibleRole.PRESENTATION,
            )
        )
        timer = label("", "live-timer")
        show = Gtk.Button(child=timer, css_classes=["flat"], tooltip_text=text("recording.pill.noSleep"))
        show.connect("clicked", lambda *_: self.open_live_recording())
        live.append(show)
        picture = SpectrogramStrip(kind="pill", lanes=("me", "others"))
        live.append(picture)
        muted = Gtk.Image(
            icon_name="dialog-warning-symbolic", visible=False, tooltip_text=text("recording.systemMuted")
        )
        muted.update_property([Gtk.AccessibleProperty.LABEL], [text("recording.systemMuted")])
        live.append(muted)
        stop = Gtk.Button(label=text("common.stop"), sensitive=False, css_classes=["static-stop"])
        stop._meeting_key = meeting.key if meeting else None
        stop.connect("clicked", self._stop_live)
        live.append(stop)
        stack.add_named(live, "live")
        stack._controls = dict(
            stack=stack,
            live=live,
            timer=timer,
            picture=picture,
            stop=stop,
            muted=muted,
            meeting=meeting.key if meeting else None,
        )
        self._capture_headers.append(stack._controls)
        return stack

    def _queue_sidebar_scrolled(self, *_args):
        if not self._sidebar_scroll_source:
            self._sidebar_scroll_source = GLib.idle_add(self._apply_sidebar_scrolled)

    def _apply_sidebar_scrolled(self):
        self._sidebar_scroll_source = 0
        if hasattr(self, "sidebar_header"):
            vadj = (
                self.search_page._vadj
                if self.search_page is not None and hasattr(self.search_page, "_vadj")
                else getattr(self, "_library_vadj", None)
            )
            scrolled = bool(vadj and vadj.get_value() > 0.5)
            (self.sidebar_header.add_css_class if scrolled else self.sidebar_header.remove_css_class)("scrolled")
        return GLib.SOURCE_REMOVE

    def _on_sidebar_search_changed(self, *_args):
        if not self._closing_search and self.search_page is None and self.search_entry.get_text():
            self._enter_search(focus=False)

    def _on_sidebar_search_clicked(self, *_args):
        if not self._closing_search and self.search_page is None:
            self._enter_search(focus=True)

    def _library_filter_row(self):
        """L-34 at rest: linked toggle buttons All / Starred; hidden with the list while searching."""
        settings = getattr(self.get_application(), "recording_settings", None)
        remembered = settings.get_string("library-filter") if settings is not None else "all"
        self.library_filter = "starred" if remembered == "starred" else "all"
        group = Gtk.Box(
            css_classes=["linked", "library-filters"], halign=Gtk.Align.START, accessible_role=Gtk.AccessibleRole.GROUP
        )
        group.update_property([Gtk.AccessibleProperty.LABEL], [text("library.filters.a11y")])
        self.filter_all = Gtk.ToggleButton(label=text("library.filter.all"), active=self.library_filter == "all")
        self.filter_starred = Gtk.ToggleButton(
            label=text("library.filter.starred"), group=self.filter_all, active=self.library_filter == "starred"
        )
        for button, value in ((self.filter_all, "all"), (self.filter_starred, "starred")):
            button.add_css_class("library-filter")
            button.connect(
                "toggled", lambda toggle, choice=value: self.set_library_filter(choice) if toggle.get_active() else None
            )
            group.append(button)
        self.library_filter_row = Gtk.Box(css_classes=["library-filter-row"])
        self.library_filter_row.append(group)
        return self.library_filter_row

    def _build_library(self):
        self.split = Adw.NavigationSplitView(min_sidebar_width=320, max_sidebar_width=400, sidebar_width_fraction=0.53)
        self.split.set_min_sidebar_width(320)
        self.split.set_max_sidebar_width(400)
        self.split.set_sidebar_width_unit(Adw.LengthUnit.PX)
        sidebar = Adw.ToolbarView(css_classes=["library-sidebar"])
        header = self.sidebar_header = Adw.HeaderBar(
            show_start_title_buttons=False, show_end_title_buttons=False, css_classes=["library-header"]
        )
        header.set_title_widget(Gtk.Box())
        header.pack_start(self._primary_menu_button(flat=True))
        self.capture_header = self.capture_controls()
        controls = self.capture_header._controls
        self.live_timer, self.live_stop = controls["timer"], controls["stop"]
        self.import_audio_button = self.import_button()
        header_actions = Gtk.Box(spacing=4, valign=Gtk.Align.CENTER)
        header_actions.append(self.import_audio_button)
        header_actions.append(self.capture_header)
        header.pack_end(header_actions)
        sidebar.add_top_bar(header)

        sidebar_content = vertical(vexpand=True, hexpand=True)
        search_box = Gtk.Box(css_classes=["sidebar-search-box"])
        self.search_entry = Gtk.SearchEntry(
            placeholder_text=text("search.field.placeholder"),
            hexpand=True,
            css_classes=["sidebar-search-entry"],
        )
        self.search_entry.update_property([Gtk.AccessibleProperty.LABEL], [text("search.field.a11y")])
        self.protect_text_input(self.search_entry)
        self.search_entry.connect("search-changed", self._on_sidebar_search_changed)
        search_click = Gtk.GestureClick()
        search_click.connect("pressed", self._on_sidebar_search_clicked)
        self.search_entry.add_controller(search_click)
        search_box.append(self.search_entry)
        sidebar_content.append(search_box)

        self.sidebar_stack = Gtk.Stack(vexpand=True, hexpand=True, hhomogeneous=False, vhomogeneous=False)
        self.rows = Gtk.ListBox(
            selection_mode=Gtk.SelectionMode.SINGLE, activate_on_single_click=False, css_classes=["library-rows"]
        )
        self.rows.connect(
            "row-activated", lambda _rows, row: self.open_meeting(row.meeting) if hasattr(row, "meeting") else None
        )
        self._selection_handler = self.rows.connect("row-selected", self._selected)
        self.rows.set_filter_func(self._row_visible)
        # After _selected: an unstarred meeting left open hides once selection moves.
        self.rows.connect(
            "row-selected", lambda *_: self.rows.invalidate_filter() if self.library_filter == "starred" else None
        )
        list_content = vertical()
        self.recording_lines = vertical(margin_start=14, margin_end=14, margin_top=8, visible=False)
        self._recording_line_signature = None
        list_content.append(self.recording_lines)
        list_content.append(self.rows)
        self.starred_empty = label(
            text("library.starred.empty"),
            "dim-label",
            "library-starred-empty",
            wrap=True,
            justify=Gtk.Justification.CENTER,
            margin_start=20,
            margin_end=20,
            margin_top=32,
            margin_bottom=32,
            visible=False,
        )
        list_content.append(self.starred_empty)
        self.received_hint = label(
            text("received.libraryHint"),
            "small",
            "dim-label",
            wrap=True,
            xalign=0,
            margin_start=20,
            margin_end=20,
            margin_top=14,
            margin_bottom=14,
            visible=False,
        )
        list_content.append(self.received_hint)

        self.library_scroller = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER, child=list_content, vexpand=True
        )
        self._library_vadj = self.library_scroller.get_vadjustment()
        self._library_vadj.connect("value-changed", self._queue_sidebar_scrolled)

        self.drop_frame = Gtk.Overlay(
            child=self.library_scroller, vexpand=True, hexpand=True, css_classes=["drop-list-frame"]
        )
        self.drop_overlay = vertical(
            8,
            halign=Gtk.Align.FILL,
            valign=Gtk.Align.FILL,
            can_target=False,
            visible=False,
            css_classes=["drop-overlay"],
        )
        drop_center = vertical(6, halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER, vexpand=True, hexpand=True)
        self.drop_overlay_glyph = BadgeGlyph("incoming", size_px=28)
        self.drop_overlay_glyph.add_css_class("drop-overlay-glyph")
        self.drop_overlay_title = label(
            text("import.drop.over"),
            "drop-overlay-title",
            justify=Gtk.Justification.CENTER,
            wrap=True,
        )
        self.drop_overlay_detail = label(
            import_feedback.accepted_formats(),
            "small",
            "dim-label",
            justify=Gtk.Justification.CENTER,
            wrap=True,
        )
        drop_center.append(self.drop_overlay_glyph)
        drop_center.append(self.drop_overlay_title)
        drop_center.append(self.drop_overlay_detail)
        self.drop_overlay.append(drop_center)
        self.drop_frame.add_overlay(self.drop_overlay)

        self.drop_footer = Gtk.Box(
            spacing=6,
            halign=Gtk.Align.FILL,
            valign=Gtk.Align.CENTER,
            css_classes=["drop-footer"],
        )
        footer_inner = Gtk.Box(spacing=6, halign=Gtk.Align.CENTER, hexpand=True, valign=Gtk.Align.CENTER)
        footer_inner.append(BadgeGlyph("incoming", size_px=14))
        self.drop_footer_label = label(text("import.drop.rest"), "small", ellipsize=Pango.EllipsizeMode.END)
        footer_inner.append(self.drop_footer_label)
        self.drop_footer.append(footer_inner)

        library_pane = vertical(vexpand=True, hexpand=True)
        library_pane.append(self._library_filter_row())
        library_pane.append(self.drop_frame)
        library_pane.append(self.drop_footer)
        self.sidebar_stack.add_named(library_pane, "library")
        self.search_sidebar_slot = Adw.Bin(vexpand=True, hexpand=True)
        self.sidebar_stack.add_named(self.search_sidebar_slot, "search")
        self.sidebar_stack.set_visible_child_name("library")
        sidebar_content.append(self.sidebar_stack)
        sidebar.set_content(sidebar_content)
        from .browser_status import BrowserStatus

        self.browser_status = BrowserStatus(self.get_application(), self)
        sidebar.add_bottom_bar(self.browser_status)
        self.split.set_sidebar(Adw.NavigationPage.new(sidebar, text("library.listPane.a11y")))

        self.detail_stack = Gtk.Stack(vexpand=True, hexpand=True, hhomogeneous=False, vhomogeneous=False)
        empty_pane = vertical(vexpand=True, hexpand=True, css_classes=["meeting-empty-pane"])
        empty_bar = Adw.HeaderBar(
            show_start_title_buttons=False,
            show_back_button=False,
            show_end_title_buttons=True,
            decoration_layout=":close",
            show_title=False,
            css_classes=["meeting-topbar"],
        )
        empty_pane.append(empty_bar)
        self.empty_selection_label = label(
            text("meeting.emptySelection"),
            "meeting-empty-selection",
            halign=Gtk.Align.CENTER,
            valign=Gtk.Align.CENTER,
            vexpand=True,
            hexpand=True,
        )
        empty_pane.append(self.empty_selection_label)
        self.detail_stack.add_named(empty_pane, "empty")
        self.meeting_slot = Adw.Bin(vexpand=True, hexpand=True)
        self.detail_stack.add_named(self.meeting_slot, "meeting")
        self.recording_slot = Adw.Bin(vexpand=True, hexpand=True)
        self.detail_stack.add_named(self.recording_slot, "recording")
        self.search_detail_slot = Adw.Bin(vexpand=True, hexpand=True)
        self.detail_stack.add_named(self.search_detail_slot, "search")
        self.detail_stack.set_visible_child_name("empty")

        self.split.set_content(Adw.NavigationPage.new(self.detail_stack, text("library.previewPane.a11y")))
        responsive = self.breakpoint_bin = Adw.BreakpointBin(child=self.split, width_request=360, height_request=100)
        narrow = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 760sp"))
        narrow.add_setter(self.split, "collapsed", True)
        responsive.add_breakpoint(narrow)
        self.root_box.add_named(responsive, "library")

    def open_pending_recording(self):
        """Open the recording the application just started, once this Library has it live.

        Only a row scanned as recording (or saving) opens it: its Recording
        page. Sides open after the folder exists (R79 §1), so a scan made
        before the recorder was active can show the row as an ordinary
        meeting, and the activity tick replays that snapshot: opening that
        row would show a meeting page instead. A recording that ends before
        any live scan clears its pending path when it finishes.
        """
        app = self.get_application()
        pending = app._pending_recording
        if not pending or self.snapshot is None:
            return False
        current = next((m for m in self.snapshot.meetings if str(m.directory) == pending), None)
        if current is None or current.lifecycle not in ("recording", "saving"):
            return False
        app._pending_recording = None
        self.open_meeting(current)
        return True

    def capture_changed(self):
        # Level messages arrive at 10 Hz. Update meters, scanning only on a
        # controller phase change; date grouping stays on the one-second tick.
        activity = application_activity(self.get_application(), self._activity_source, self._fixture_activity)
        if activity != self._activity:
            self._activity = activity
            if self.snapshot is not None:
                self._updated(getattr(self, "_source_snapshot", None) or self.snapshot)
            self.monitor.refresh_now()
        self._update_live()

    def companion_available_changed(self):
        available = self.get_application().browser_companion_available
        self.first_companion.set_visible(False)
        if available and self._activity_source is None and os.environ.get("CASSINI_SCREENSHOT_ISOLATED") != "1":
            self._activity_source = BrowserActivity(self._activity_tick)
        if self.meeting_page:
            self.meeting_page.set_connected(self._activity_source.connected if self._activity_source else False)
        self.browser_connection_changed()

    def browser_connection_changed(self):
        self.first_browser_status.render()
        self.browser_status.render()

    def _activity_tick(self):
        refresh = self.get_application().build_refresh
        if refresh:
            refresh.capture_changed()
        activity = application_activity(self.get_application(), self._activity_source, self._fixture_activity)
        if activity != self._activity:
            self._activity = activity
            self.monitor.refresh_now()
        self._update_live()
        if self.meeting_page and self._activity_source:
            self.meeting_page.set_connected(self._activity_source.connected)
        # Re-group at local midnight without touching the filesystem.
        if self.snapshot is not None:
            self._updated(self._source_snapshot or self.snapshot)
        return GLib.SOURCE_CONTINUE

    def _update_live(self):
        if self.snapshot is None:
            return
        app = self.get_application()
        live = self.live_meeting()
        native = getattr(app, "recorder", None)
        real_native = native is not None and native.pipeline is not None
        if real_native:
            elapsed = max(0, (GLib.get_monotonic_time() - native._start_us) // 1000)
        else:
            elapsed = max(0, self.now_ms - live.started_at_ms) if live and live.started_at_ms else 0
        browser_stops = getattr(app, "_browser_stops", ())
        stop_all_browser = None in browser_stops
        stopping_ids = {
            identifier for entry in browser_stops if isinstance(entry, str) and (identifier := meeting_id(Path(entry)))
        }
        for controls in self._capture_headers:
            target = (
                next((m for m in self.snapshot.meetings if m.key == controls["meeting"]), None)
                if controls["meeting"]
                else (
                    next(
                        (m for m in self.snapshot.meetings if m.key == self.recording_page.meeting.key),
                        self.recording_page.meeting,
                    )
                    if self.recording_page is not None and not self._external_meeting
                    else live
                )
            )
            local_native = app.comfort._native_for(target) if target else native
            local_stopping = recorder_stopping(local_native)
            own_native = local_native is not None and (local_native.pipeline is not None or local_stopping)
            browser = bool(
                target
                and target.lifecycle == "recording"
                and self._activity_source
                and self._activity.get(target.id, self._activity_source.snapshot.get(target.id)) == "recording"
            )
            stopping = bool(
                local_stopping
                or (
                    target
                    and (
                        target.lifecycle == "saving"
                        or self._activity.get(target.id) == "saving"
                        or stop_all_browser
                        or str(target.directory) in browser_stops
                        or target.id in stopping_ids
                    )
                )
                or (controls["meeting"] is None and target is None and bool(browser_stops))
            )
            ms = app.comfort.elapsed(target) if target else elapsed
            controls["stack"].set_visible_child_name("live" if target or own_native or stopping else "record")
            controls["timer"].set_label(fmt.live_badge(ms))
            controls["stop"].set_label(text("states.badge.saving" if stopping else "common.stop"))
            controls["stop"].set_sensitive(not stopping and (own_native or browser))
            (controls["live"].add_css_class if stopping else controls["live"].remove_css_class)("saving")
            state = app.comfort.status(target) if target else {}
            controls["muted"].set_visible(bool(state.get("output_muted")))
            self.bind_spectrogram(controls["picture"], target, silent=bool(state.get("silent")), stopping=stopping)
        self._recording_line(native)
        if self.recording_page:
            self.recording_page.update_status()
        for key, badge_label in list(self._live_badges.items()):
            meeting = next((m for m in self.snapshot.meetings if m.key == key), None)
            if meeting and meeting.lifecycle == "recording":
                ms = self.get_application().comfort.elapsed(meeting)
                badge_label.set_label(meeting.badge(live_ms=ms).text)

    def stop_meeting(self, meeting):
        app = self.get_application()
        if meeting.capture_source == "gnome":
            app.stop_native(directory=meeting.directory)
        else:
            app.stop_browser_recordings(meeting.directory)

    def live_meeting(self):
        if not self.snapshot:
            return None
        live = [m for m in self.snapshot.meetings if m.lifecycle in ("recording", "saving")]
        return (
            min(live, key=lambda m: (m.lifecycle != "recording", m.started_at_ms is None, m.started_at_ms or 0, m.key))
            if live
            else None
        )

    def open_live_recording(self):
        meeting = self.live_meeting()
        if meeting:
            self.open_meeting(meeting)

    def _stop_live(self, button=None):
        key = getattr(button, "_meeting_key", None)
        if key:
            meeting = next((m for m in self.snapshot.meetings if m.key == key), None)
            if meeting:
                self.stop_meeting(meeting)
            return
        self.get_application().on_stop_recording()

    def _recording_line(self, recorder):
        app = self.get_application()
        message = getattr(app, "recording_error", None)
        failure = getattr(app, "recording_failure", None)
        banner = (failure["id"], tuple(failure.get("captions", ())), failure["action"]) if failure else None
        # D17 (R82 §2): one line per meeting that failed while others started.
        pending = getattr(app, "_record_start_future", None) is not None
        meetings = tuple(
            (entry["id"], entry.get("callGeneration"), entry.get("title"), entry.get("caption"), pending)
            for entry in getattr(app, "recording_failures", ())
        )
        shown_in_right_recording = bool(
            self.recording_page is not None
            and not self._external_meeting
            and self.selected is not None
            and self.recording_page.meeting.key == self.selected.key
            and not (
                hasattr(self, "split")
                and self.split is not None
                and self.split.get_collapsed()
                and not self.split.get_show_content()
            )
        )
        selected = app.comfort.status(self.selected) if self.selected and not shown_in_right_recording else {}
        live = bool(
            self.selected and not shown_in_right_recording and self.selected.lifecycle in ("recording", "saving")
        )
        fallback = bool(live and app.comfort.sources(self.selected).fallback)
        refused = tuple(tuple(sorted(line.items())) for line in self._refusal_lines(self.selected)) if live else ()
        signature = selected.get("line"), selected.get("minutes", 0), message, fallback, banner, refused, meetings
        if signature != getattr(self, "_recording_line_signature", None):
            self._recording_line_signature = signature
            lines = getattr(self, "recording_lines", None)
            if lines is not None:
                lines.set_visible(bool(signature[0] or message or fallback or banner or refused or meetings))
                while child := lines.get_first_child():
                    lines.remove(child)
                self._fill_recording_lines(
                    lines, signature[0], signature[1], message, banner, fallback, refused, meetings
                )
        first = (
            (recorder.line, recorder.minutes_left, message, banner, meetings)
            if recorder
            else (
                None,
                0,
                message,
                banner,
                meetings,
            )
        )
        if first != getattr(self, "_first_recording_line_signature", None):
            self._first_recording_line_signature = first
            self.first_recording_lines.set_visible(bool(first[0] or message or banner or meetings))
            while child := self.first_recording_lines.get_first_child():
                self.first_recording_lines.remove(child)
            self._fill_recording_lines(
                self.first_recording_lines, first[0], first[1], message, banner, meetings=meetings
            )
        if self.meeting_page:
            state = app.comfort.status(self.meeting_page.meeting)
            self.meeting_page.set_recording_notice(state["line"], state["minutes"])

    def _refusal_lines(self, meeting):
        """D58: the selected live recording's refused sides, from its live session."""
        from .refusals import live_lines

        native = self.get_application().comfort._native_for(meeting)
        if native is not None and native.session is not None:
            manifest = native.session.manifest
            tracks = [(t.get("kind"), t.get("ended"), t.get("startOffsetMs")) for t in manifest.get("tracks", ())]
            return live_lines(manifest.get("refusals"), tracks, manifest.get("events") or (), native.armed_sides())
        tracks = [(t.kind, t.ended, t.start_offset_ms) for t in meeting.tracks]
        return live_lines(list(meeting.capture_refusals), tracks, meeting.capture_events, self._armed_sides(meeting))

    def _armed_sides(self, meeting):
        """D58.1: a browser capture's sides waiting for their first buffer, from
        its live status (the folder cannot say so)."""
        if getattr(meeting, "id", None) is None:
            return None
        snapshots = getattr(self, "_browser_snapshot", {}), getattr(self.get_application(), "_browser_snapshot", {})
        for snapshot in snapshots:
            for status in snapshot.get("statuses", ()) if isinstance(snapshot, dict) else ():
                if (
                    isinstance(status, dict)
                    and isinstance(status.get("path"), str)
                    and meeting_id(Path(status["path"])) == meeting.id
                    and isinstance(status.get("armed"), dict)
                ):
                    return status["armed"]
        return None

    def _fill_recording_lines(self, box, line, minutes, message, banner, fallback=False, refused=(), meetings=()):
        from .inline_line import InlineLine

        app = self.get_application()
        if line:
            box.append(InlineLine(line, n=minutes))
        if banner:
            # Nothing opened (D58): catalog copy and Try again, never raw error text.
            # Every distinct caption, in first-occurrence order (R82 decision 6).
            identifier, captions, action = banner
            box.append(InlineLine(identifier, action=action, callback=app.retry_recording))
            for caption in captions:
                box.append(label(text(caption), "caption", "dim-label", wrap=True, xalign=0))
        for identifier, generation, title, caption, pending in meetings:
            # D17 (R82 §2): this meeting's line and its own Try again, insensitive
            # while any start is pending.
            entry = dict(id=identifier, callGeneration=generation, title=title, caption=caption)
            box.append(
                InlineLine(
                    "errors.recordFailed.meeting",
                    title=(title or "").strip() or text("library.untitled"),
                    action="common.tryAgain",
                    callback=None if pending else lambda entry=entry: app.retry_meeting(entry),
                )
            )
            if caption:
                box.append(label(text(caption), "caption", "dim-label", wrap=True, xalign=0))
        if message:
            box.append(label(message, wrap=True, xalign=0))
        self.append_refusal_lines(box, refused, fallback)

    def append_refusal_lines(self, box, refused, fallback=False, meeting=None):
        """D58 live lines: one per refused side; a browser refusal replaces Why?."""
        from .inline_line import InlineLine

        refused = [dict(item) for item in refused]
        browser = next((item for item in refused if item["side"] == "browser"), None)
        for item in refused:
            if item["side"] == "browser":
                continue
            # Try again for every line but the microphone's permission line (D58.1:
            # a refused computer sound has one line, whatever its code).
            retry = (
                None
                if item["id"] == "errors.micPermission.line"
                else self._turn_on(item["side"], meeting or self.selected)
            )
            box.append(InlineLine(item["id"], action="common.tryAgain" if retry else None, callback=retry))
        if browser is not None:
            # A browser refusal: its caption, if any, instead of Why? (R79 §3).
            box.append(InlineLine("errors.othersFallback.line", info=True))
            if browser["caption"]:
                box.append(label(text(browser["caption"]), "caption", "dim-label", wrap=True, xalign=0))
        elif fallback and not any(item["side"] == "others" for item in refused):
            # A refused computer-sound side has its own line; never claim it records.
            from .what_is_recorded import explain_fallback

            box.append(
                InlineLine(
                    "errors.othersFallback.line",
                    action="errors.othersFallback.action",
                    callback=lambda: explain_fallback(self),
                    info=True,
                )
            )

    def _turn_on(self, side, meeting):
        """Try again for a refused native side: a turn-on.

        The native recorder reopens a selected side without a track. A browser
        capture reopens it through selection {retry: true} (R82 1c).
        """
        app = self.get_application()
        if meeting is None:
            return None
        native = app.comfort._native_for(meeting) is not None
        current = app.comfort.sources(meeting)

        def retry():
            # A change already in progress raises ValueError; the line stays.
            with contextlib.suppress(ValueError):
                me, others = current.me or side == "me", current.others or side == "others"
                if native:
                    app.change_recording_sources(meeting, me, others)
                else:
                    app.change_recording_sources(meeting, me, others, retry=True)

        return retry

    def _row_display_fields(self, meeting):
        duration = (
            fmt.meeting_duration(meeting.duration_ms)
            if meeting.lifecycle == "saved" and meeting.duration_ms is not None
            else None
        )
        if meeting.lifecycle == "recording":
            secondary = meeting.row_texts()[0] or ""
        elif meeting.lifecycle != "saved" or meeting.transcript in ("queued", "running"):
            secondary = ""
        elif meeting.speaker_names:
            names = meeting.speaker_names
            secondary = ", ".join(names[:3]) + (f" +{len(names) - 3}" if len(names) > 3 else "")
        else:
            secondary = ""
        return duration, secondary

    def _row_signature(self, meeting):
        badge = meeting.badge()
        return (
            meeting.key,
            meeting.title,
            fmt.row_time(meeting.display_start, tz=self.tz),
            badge.kind if badge else None,
            badge.text if badge and meeting.lifecycle != "recording" else None,
            LibraryWindow._row_display_fields(self, meeting),
            LibraryWindow.is_starred(self, meeting),
        )

    def _active_import_list(self):
        active = getattr(self, "_active_imports", None) or {}
        return tuple(entry for entries in active.values() for entry in entries)

    def _failed_import_list(self):
        """W4.6: failed imports, in memory only, as (stem, filename, failed_ms, path)."""
        return tuple((getattr(self, "_failed_imports", None) or {}).values())

    def _has_import_cards(self):
        return bool(LibraryWindow._active_import_list(self) or LibraryWindow._failed_import_list(self))

    def _resync_import_cards(self):
        if getattr(getattr(self, "monitor", None), "_closed", False):
            return
        if self._list_signature and self._list_signature[0] == "library":
            LibraryWindow._sync_import_rows(self, LibraryWindow._active_import_list(self))
            if hasattr(self, "sidebar_header"):
                self._queue_sidebar_scrolled()
        elif self.snapshot is not None:
            self._updated(self._source_snapshot or self.snapshot)

    def _add_failed_imports(self, paths):
        """Keep a Needs attention card for each failed top-level import (W4.6)."""
        failed = getattr(self, "_failed_imports", None)
        if failed is None:
            failed = self._failed_imports = {}
        now = self.now_ms
        added = False
        for raw in paths or ():
            if not raw:
                continue
            path = Path(raw)
            if LibraryWindow._is_folder_open_path(path):
                continue
            # Newest failure last; a repeated failure moves its card.
            failed.pop(str(path), None)
            failed[str(path)] = (path.stem or path.name, path.name, now, str(path))
            added = True
        if added:
            LibraryWindow._resync_import_cards(self)

    def _dismiss_failed_import(self, raw):
        failed = getattr(self, "_failed_imports", None)
        if failed and failed.pop(raw, None) is not None:
            LibraryWindow._resync_import_cards(self)

    def _retry_failed_import(self, raw):
        LibraryWindow._dismiss_failed_import(self, raw)
        self.open_files((Gio.File.new_for_path(raw),))

    def _clear_import_rows(self):
        widgets = getattr(self, "_import_row_widgets", None)
        if widgets:
            for widget in widgets:
                get_parent = getattr(widget, "get_parent", None)
                if get_parent is None or get_parent() is self.rows:
                    self.rows.remove(widget)
            widgets.clear()
        self._rendered_imports = ()

    def _sync_import_rows(self, active_imports):
        today_title = fmt.day_group(self.now_ms, now_ms=self.now_ms, tz=self.tz)
        has_today_group = bool(self._rows_layout and self._rows_layout[0][0] == today_title)
        failed_imports = LibraryWindow._failed_import_list(self)
        target = (has_today_group, today_title, active_imports, failed_imports)
        if getattr(self, "_rendered_imports", ()) == target:
            return
        LibraryWindow._clear_import_rows(self)
        if not active_imports and not failed_imports:
            return
        widgets = []
        insert_at = 1 if has_today_group else 0
        if not has_today_group:
            heading = self._day_heading(today_title)
            self.rows.insert(heading, 0)
            widgets.append(heading)
            insert_at = 1
        for offset, (stem, filename, started_ms) in enumerate(active_imports):
            row = self._importing_row(stem, filename, started_ms)
            self.rows.insert(row, insert_at + offset)
            widgets.append(row)
        insert_at += len(active_imports)
        for offset, (stem, filename, failed_ms, raw) in enumerate(failed_imports):
            row = self._failed_import_row(stem, filename, failed_ms, raw)
            self.rows.insert(row, insert_at + offset)
            widgets.append(row)
        self._import_row_widgets = widgets
        self._rendered_imports = target

    def _begin_import_entries(self, token, paths):
        entries = []
        now = self.now_ms
        for raw in paths or ():
            if not raw:
                continue
            path = Path(raw)
            if not LibraryWindow._is_folder_open_path(path):
                entries.append((path.stem or path.name, path.name, now))
                # Trying the same file again replaces its failed card.
                failed = getattr(self, "_failed_imports", None)
                if failed:
                    failed.pop(str(path), None)
        if entries:
            self._active_imports[token] = tuple(entries)
            if self._list_signature and self._list_signature[0] == "library":
                LibraryWindow._sync_import_rows(self, LibraryWindow._active_import_list(self))
                if hasattr(self, "sidebar_header"):
                    self._queue_sidebar_scrolled()
            elif self.snapshot is not None:
                self._updated(self._source_snapshot or self.snapshot)

    def _finish_import_entries(self, token):
        closed = getattr(getattr(self, "monitor", None), "_closed", False)
        if self._active_imports.pop(token, None) is not None and not closed:
            if self._list_signature and self._list_signature[0] == "library":
                LibraryWindow._sync_import_rows(self, LibraryWindow._active_import_list(self))
                if hasattr(self, "sidebar_header"):
                    self._queue_sidebar_scrolled()
            elif self.snapshot is not None:
                self._updated(self._source_snapshot or self.snapshot)

    def _is_saved_detail_ready(self, meeting):
        if meeting is None or meeting.lifecycle != "saved":
            return False
        app = self.get_application()
        if app is not None and str(meeting.directory) == getattr(app, "_pending_recording", None):
            return False
        return not (self.search_page or self._external_meeting)

    def _is_live_detail_ready(self, meeting):
        if meeting is None or meeting.lifecycle not in ("recording", "saving"):
            return False
        app = self.get_application()
        if app is not None:
            if str(meeting.directory) == getattr(app, "_pending_recording", None):
                return False
            comfort = getattr(app, "comfort", None)
            native = comfort._native_for(meeting) if comfort is not None and hasattr(comfort, "_native_for") else None
            if native is not None and getattr(native, "starting", False):
                return False
        return not (self.search_page or self._external_meeting)

    def _show_right_recording(self, meeting):
        from .recording_page import RecordingPage

        LibraryWindow._close_right_meeting(self, keep_content=True)
        if (
            self.recording_page is None
            or self.recording_page.get_parent() is not self.recording_slot
            or self.recording_page.meeting.key != meeting.key
        ):
            if self.recording_page is not None and not self._external_meeting:
                self.recording_page.close()
            self.recording_page = RecordingPage(self, meeting)
            self.recording_slot.set_child(self.recording_page)
        else:
            self.recording_page.update(meeting)
        self.detail_stack.set_visible_child_name("recording")

    def _close_right_recording(self, *, keep_content=False):
        if self.recording_page is not None and not self._external_meeting:
            self.recording_page.close()
            self.recording_page = None
        if hasattr(self, "recording_slot"):
            self.recording_slot.set_child(None)
        if not keep_content and not self.meeting_page and not self.search_page:
            if hasattr(self, "detail_stack"):
                self.detail_stack.set_visible_child_name("empty")
            if hasattr(self, "split"):
                self.split.set_show_content(False)

    def _show_right_meeting(self, meeting, *, rebind_player=False, reset_scroll=False):
        LibraryWindow._close_right_recording(self, keep_content=True)
        if self.meeting_page is None or self.meeting_page.get_parent() is not self.meeting_slot:
            if self.meeting_page is not None and not self._external_meeting:
                self.meeting_page.close()
            self.meeting_page = MeetingPage(self, meeting)
            self.meeting_slot.set_child(self.meeting_page)
        else:
            if rebind_player and self.meeting_page.player is not self.player:
                self.meeting_page.rebind_player(self.player)
            self.meeting_page.select_meeting(meeting, reset_scroll=reset_scroll)
        self.detail_stack.set_visible_child_name("meeting")

    def _close_right_meeting(self, *, keep_content=False):
        if self.meeting_page is not None and not self._external_meeting:
            self.meeting_page.close()
            self.meeting_page = None
        if hasattr(self, "meeting_slot"):
            self.meeting_slot.set_child(None)
        if not keep_content and not self.recording_page and not self.search_page:
            if hasattr(self, "detail_stack"):
                self.detail_stack.set_visible_child_name("empty")
            if hasattr(self, "split"):
                self.split.set_show_content(False)

    def _project_activity(self, snapshot):
        meetings = tuple(
            replace(m, lifecycle="saving")
            if m.origin == "own" and m.lifecycle == "recording" and self._activity.get(m.id) == "saving"
            else m
            for m in snapshot.meetings
        )
        return replace(snapshot, meetings=meetings) if meetings != snapshot.meetings else snapshot

    def _updated(self, snapshot):
        snapshot = self._project_activity(snapshot)
        if recovery := getattr(self.get_application(), "library_recovery", None):
            recovery.observe(snapshot)
            snapshot = recovery.project(snapshot)
        snapshot = self.get_application().meeting_trash.project(snapshot)
        fresh_scan = snapshot is not self.snapshot
        self.get_application().observe_library(snapshot, self._observed_phases)
        self._observed_phases = {
            m.key: (m.lifecycle, m.capture_state, m.published_file is not None) for m in snapshot.meetings
        }
        self.snapshot = snapshot
        from .library_location import initial_root, music_directory, primary

        # This published caption names Music/Cassini literally. An unmounted
        # Music directory uses private persistent app data in Flatpak instead.
        default = initial_root()
        music_caption = (
            os.environ.get("CASSINI_LIBRARY_DIR") is not None or default == (music_directory() / "Cassini").absolute()
        )
        self.privacy_footer.set_visible(primary() == default and music_caption)
        loaded = next((m for m in snapshot.meetings if m.key == self._loaded_key), None)
        if loaded and not self._external_meeting and self._loaded_directory != loaded.directory:
            position, playing = self.player.position, self.player.playing
            if self._ensure_player(loaded):
                self.player.seek(position)
                if playing:
                    self.player.play()
        if self.search_page:
            self.search_page.update(snapshot)
        if self.recording_page:
            current = next((m for m in snapshot.meetings if m.key == self.recording_page.meeting.key), None)
            if current and (current.lifecycle in ("recording", "saving") or self.get_application()._quitting):
                self.recording_page.update(current)
            elif current:
                if self._external_meeting:
                    self.back_to_library()
                else:
                    LibraryWindow._close_right_recording(self, keep_content=True)
                self.open_meeting(current)
            else:
                if self._external_meeting:
                    self.back_to_library()
                else:
                    LibraryWindow._close_right_recording(self)
                    self._stop_playback(clear_selection=True)
        self.open_pending_recording()
        if self.meeting_page and not self._external_meeting:
            current = next((m for m in snapshot.meetings if m.key == self.meeting_page.meeting.key), None)
            if current is None:
                LibraryWindow._close_right_meeting(self)
                self._stop_playback(clear_selection=True)
            else:
                if not current.has_audio:
                    self._stop_playback()
                self.meeting_page.update(current)
        if self.export_dialog and not self.export_dialog.closed:
            current = next((m for m in snapshot.meetings if m.key == self.export_dialog.meeting.key), None)
            if current and not (self.meeting_page and self._external_meeting):
                self.export_dialog.update(current)
        if self.zip_export_dialog and not self.zip_export_dialog.closed:
            self.zip_export_dialog.update_snapshot(snapshot)
        if self.rename_dialog and not self.rename_dialog.closed:
            current = next((m for m in snapshot.meetings if m.key == self.rename_dialog.meeting.key), None)
            if current is not None and self.rename_allowed(current):
                self.rename_dialog.update(current)
            else:
                self.rename_dialog.force_close()
        self._update_live()
        self._refresh_export()
        self.received_hint.set_visible(
            bool(snapshot.meetings) and all(m.origin == "received" for m in snapshot.meetings)
        )
        if (
            self._pending_selection
            and fresh_scan
            and not self._opening_files
            and not any(m.key == self._pending_selection for m in snapshot.meetings)
        ):
            self._pending_selection = None
        active_imports = LibraryWindow._active_import_list(self)
        import_cards = bool(active_imports or LibraryWindow._failed_import_list(self))
        pending_row = any(m.key == self._pending_selection for m in snapshot.meetings)
        if snapshot.unavailable:
            if not pending_row and self._list_signature == ("offline",):
                self._pending = False
                return
            groups, layout, group_layout_key = (), None, None
        elif not snapshot.meetings and not import_cards:
            if not pending_row and self._list_signature == ("empty",) and not self.search_page:
                self._pending = False
                return
            groups, layout, group_layout_key = (), None, None
        elif not snapshot.meetings:
            if self._list_signature == ("empty_importing",):
                LibraryWindow._sync_import_rows(self, active_imports)
                self._pending = False
                if hasattr(self, "sidebar_header"):
                    self._queue_sidebar_scrolled()
                return
            groups, layout, group_layout_key = (), (), None
        else:
            group_layout_key = (
                fmt._local(self.now_ms, self.tz).date(),
                self.tz,
                tuple((m.key, m.display_start) for m in snapshot.meetings),
            )
            if group_layout_key == self._group_layout_key and self._rows_layout is not None:
                groups = None
                layout = self._rows_layout
            else:
                groups = snapshot.groups(now_ms=self.now_ms, tz=self.tz)
                layout = tuple((group.title, tuple(m.key for m in group.meetings)) for group in groups)
            if self._list_signature == ("library", layout):
                for meeting in snapshot.meetings:
                    row = self._row_by_key[meeting.key]
                    if row.meeting is not meeting or self.tz != self._rows_tz:
                        sig = self._row_signature(meeting)
                        if sig != row._row_signature:
                            self._fill_row(row, meeting, sig)
                        else:
                            row.meeting = meeting
                self._rows_tz = self.tz
                self._group_layout_key = group_layout_key
                LibraryWindow._sync_import_rows(self, active_imports)
                if pending_row:
                    target_row = self._row_by_key.get(self._pending_selection)
                    if target_row is not None:
                        self._pending_selection = None
                        self.rows.handler_block(self._selection_handler)
                        self.rows.select_row(target_row)
                        self.rows.handler_unblock(self._selection_handler)
                self._updating_rows_selection = True
                try:
                    self._selected(self.rows, self.rows.get_selected_row())
                finally:
                    self._updating_rows_selection = False
                self._pending = False
                LibraryWindow._apply_library_filter(self)
                if hasattr(self, "sidebar_header"):
                    self._queue_sidebar_scrolled()
                return
            if groups is None:
                groups = snapshot.groups(now_ms=self.now_ms, tz=self.tz)
        was_library = bool(self._list_signature and self._list_signature[0] == "library")
        old = self._pending_selection or (self.selected.key if self.selected else None)
        old_rows = self._row_by_key
        old_tz = self._rows_tz
        old_live_badges = getattr(self, "_live_badges", {})
        self.rows.handler_block(self._selection_handler)
        self.rows.select_row(None)
        self.rows.remove_all()
        self._import_row_widgets = []
        self._rendered_imports = ()
        self._row_by_key = {}
        self._rows_layout = layout
        self._group_layout_key = group_layout_key
        self._rows_tz = self.tz
        self._live_badges = {}
        self.selected = None
        if snapshot.unavailable:
            if not self._external_meeting and not self.search_page:
                LibraryWindow._close_right_recording(self)
                LibraryWindow._close_right_meeting(self)
                self._stop_playback(clear_selection=True)
            self.root_box.set_visible_child_name("library" if self.search_page else "offline")
            self._list_signature = ("offline",)
        elif not snapshot.meetings and not import_cards:
            if not self._external_meeting and not self.search_page:
                LibraryWindow._close_right_recording(self)
                LibraryWindow._close_right_meeting(self)
                self._stop_playback(clear_selection=True)
            self.root_box.set_visible_child_name("library" if self.search_page else "empty")
            self._list_signature = ("empty",)
        else:
            self.root_box.set_visible_child_name("library")
            selected = None
            for group in groups:
                heading = self._day_heading(group.title)
                heading._group_keys = tuple(m.key for m in group.meetings)
                self.rows.append(heading)
                for meeting in group.meetings:
                    row = old_rows.get(meeting.key)
                    if row is None:
                        row = self._row(meeting)
                    else:
                        if row.meeting is not meeting or self.tz != old_tz:
                            sig = self._row_signature(meeting)
                            if sig != row._row_signature:
                                self._fill_row(row, meeting, sig)
                            else:
                                row.meeting = meeting
                        if meeting.lifecycle == "recording" and meeting.key not in self._live_badges:
                            badge_widget = old_live_badges.get(meeting.key)
                            if badge_widget is not None:
                                self._live_badges[meeting.key] = badge_widget
                    self._row_by_key[meeting.key] = row
                    self.rows.append(row)
                    if (
                        meeting.key == old
                        or old is None
                        and not was_library
                        and meeting.title == "Budget review"
                        and self._fixed_now is not None
                    ):
                        selected = row
            LibraryWindow._sync_import_rows(self, active_imports)
            default_row = None
            if (selected is not None or old is not None or not was_library) and self._row_by_key:
                default_row = next(
                    (r for r in self._row_by_key.values() if LibraryWindow._is_saved_detail_ready(self, r.meeting)),
                    None,
                )
                self.rows.select_row(selected or default_row)
            else:
                self.rows.select_row(None)
            focus_row = (
                selected or default_row or (next(iter(self._row_by_key.values()), None) if not was_library else None)
            )
            if not was_library and not self.search_page and focus_row and hasattr(self, "search_entry"):
                current_focus = self.get_focus()
                if (
                    current_focus is None
                    or current_focus is self.search_entry
                    or current_focus.is_ancestor(self.search_entry)
                ):
                    self.set_focus(focus_row)
            if selected:
                self._pending_selection = None
            self._list_signature = ("library", layout) if snapshot.meetings else ("empty_importing",)
        self.rows.handler_unblock(self._selection_handler)
        self._updating_rows_selection = True
        try:
            self._selected(self.rows, self.rows.get_selected_row())
        finally:
            self._updating_rows_selection = False
        self.loading.set_spinning(False)
        self._pending = False
        LibraryWindow._apply_library_filter(self)
        if hasattr(self, "sidebar_header"):
            self._queue_sidebar_scrolled()

    def _day_heading(self, title):
        heading = Gtk.ListBoxRow(selectable=False, activatable=False, css_classes=["day-heading"])
        heading.set_child(label(title, "small", "day-heading-label", xalign=0))
        return heading

    def _importing_row(self, stem, filename, started_ms):
        row = Gtk.ListBoxRow(selectable=False, activatable=False, css_classes=["meeting-row", "importing-row"])
        box = vertical(6, margin_start=12, margin_end=12, margin_top=9, margin_bottom=9)
        top = Gtk.Box(spacing=8)
        top.append(label(stem, "row-title", xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END))
        box.append(top)
        meta = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
        meta.append(label(fmt.row_time(started_ms, tz=self.tz), "small", "dim-label", "row-num"))
        badge = import_entry_badge({"importing": True})
        meta.append(StatusBadge(badge.kind, badge.text))
        if filename:
            meta.append(
                label(filename, "small", "dim-label", xalign=0, ellipsize=Pango.EllipsizeMode.END, hexpand=True)
            )
        box.append(meta)
        row.set_child(box)
        return row

    def _failed_import_row(self, stem, filename, failed_ms, raw):
        """W4.6: a failed import keeps its card with the amber Needs attention badge."""
        row = Gtk.ListBoxRow(selectable=False, activatable=False, css_classes=["meeting-row", "failed-import-row"])
        box = vertical(6, margin_start=12, margin_end=12, margin_top=9, margin_bottom=9)
        top = Gtk.Box(spacing=8)
        top.append(label(stem, "row-title", xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END))
        dismiss = Gtk.Button(
            icon_name="window-close-symbolic",
            tooltip_text=text("common.close"),
            valign=Gtk.Align.CENTER,
            css_classes=["flat", "circular", "failed-import-dismiss"],
        )
        dismiss.update_property([Gtk.AccessibleProperty.LABEL], [text("common.close")])
        dismiss.connect("clicked", lambda *_: LibraryWindow._dismiss_failed_import(self, raw))
        top.append(dismiss)
        box.append(top)
        meta = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
        meta.append(label(fmt.row_time(failed_ms, tz=self.tz), "small", "dim-label", "row-num"))
        badge = import_entry_badge({"failed": True})
        meta.append(StatusBadge(badge.kind, badge.text))
        if filename:
            meta.append(
                label(filename, "small", "dim-label", xalign=0, ellipsize=Pango.EllipsizeMode.END, hexpand=True)
            )
        retry = Gtk.Button(label=text("common.tryAgain"), valign=Gtk.Align.CENTER, css_classes=["flat"])
        retry.connect("clicked", lambda *_: LibraryWindow._retry_failed_import(self, raw))
        meta.append(retry)
        box.append(meta)
        row.set_child(box)
        return row

    def _fill_row(self, row, meeting, signature=None):
        row.meeting = meeting
        row._row_signature = self._row_signature(meeting) if signature is None else signature
        self._live_badges.pop(meeting.key, None)
        box = vertical(6, margin_start=12, margin_end=12, margin_top=9, margin_bottom=9)
        top = Gtk.Box(spacing=8)
        starred = row._row_signature[-1]
        if starred:
            top.append(star_glyph())
        top.append(label(meeting.title, "row-title", xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END))
        # D99: the row reads "Starred" to assistive technology, not only by glyph.
        if starred:
            row.update_property([Gtk.AccessibleProperty.DESCRIPTION], [text("library.row.starred.a11y")])
        else:
            row.reset_property(Gtk.AccessibleProperty.DESCRIPTION)
        duration, secondary = self._row_display_fields(meeting)
        if duration:
            top.append(label(duration, "small", "dim-label", "row-num"))
        box.append(top)
        meta = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
        row_time_text = fmt.row_time(meeting.display_start, tz=self.tz)
        if row_time_text:
            meta.append(label(row_time_text, "small", "dim-label", "row-num"))
        elapsed = self.get_application().comfort.elapsed(meeting)
        badge = meeting.badge(live_ms=elapsed)
        if badge:
            badge_widget = StatusBadge(badge.kind, badge.text)
            meta.append(badge_widget)
            if meeting.lifecycle == "recording":
                self._live_badges[meeting.key] = badge_widget
        elif row_time_text and secondary:
            meta.append(label("·", "small", "dim-label", accessible_role=Gtk.AccessibleRole.PRESENTATION))
        if secondary:
            meta.append(
                label(secondary, "small", "dim-label", xalign=0, ellipsize=Pango.EllipsizeMode.END, hexpand=True)
            )
        box.append(meta)
        row.set_child(box)

    def _row(self, meeting):
        row = Gtk.ListBoxRow(css_classes=["meeting-row"])
        self._fill_row(row, meeting)
        secondary = Gtk.GestureClick(button=3)
        secondary.connect("pressed", lambda _gesture, _n, x, y: self._row_menu(row, x, y))
        row.add_controller(secondary)
        long_press = Gtk.GestureLongPress()
        long_press.connect("pressed", lambda _gesture, x, y: self._row_menu(row, x, y))
        row.add_controller(long_press)
        return row

    def _speaker(self, speaker, *, chip=False, meeting=None):
        meeting = self.selected if meeting is None else meeting
        slot = speaker_slot(speaker)
        classes = ["speaker-chip", f"speaker-chip-{slot}"] if chip else [f"speaker-ink-{slot}"]
        box = Gtk.Box(spacing=6, css_classes=classes)
        box.append(
            label(
                speaker.name,
                "speaker-name",
                ellipsize=Pango.EllipsizeMode.END,
                max_width_chars=24,
                tooltip_text=speaker.name,
            )
        )
        if (
            speaker.provenance == "recognised"
            and meeting
            and self.get_application().voice_store.has_match(meeting.directory, speaker.id)
        ):
            tag = label(text("common.autoTag"), "auto-tag", tooltip_text=text("common.autoTag.tooltip"))
            tag.update_property([Gtk.AccessibleProperty.DESCRIPTION], [text("common.autoTag.tooltip")])
            box.append(tag)
        return box

    def _selected(self, _rows, row):
        if row is None or not hasattr(row, "meeting"):
            self.selected = None
            if not self._external_meeting and not self.search_page:
                was_recording = bool(self.recording_page)
                close_rec = getattr(self, "_close_right_recording", None)
                if close_rec is not None:
                    close_rec()
                self._close_right_meeting()
                self._stop_playback(clear_selection=True)
                if was_recording and not self._updating_rows_selection:
                    update_live = getattr(self, "_update_live", None)
                    if update_live is not None:
                        update_live()
            self._recording_line(getattr(self.get_application(), "recorder", None))
            self._refresh_export()
            return
        meeting = row.meeting
        if not self._external_meeting and not self.search_page:
            was_recording = bool(self.recording_page)
            if self._preview_key != meeting.key:
                self._stop_playback()
            elif not meeting.has_audio:
                self.player.stop()
                self.listen.stop()
                self._loaded_key = None
            self._preview_key = meeting.key
            self.selected = meeting
            if LibraryWindow._is_saved_detail_ready(self, meeting):
                close_rec = getattr(self, "_close_right_recording", None)
                if close_rec is not None:
                    close_rec(keep_content=True)
                ensure_player = getattr(self, "_ensure_player", None)
                if ensure_player is not None:
                    if not self._playback_failed and not ensure_player(meeting):
                        desired = Player if meeting.audio_file is not None else FolderPlayer
                        self.listen.stop()
                        self.player.shutdown()
                        self.player = desired()
                        self.player.set_rate(self._rate)
                        self._connect_player()
                    self._show_right_meeting(meeting, rebind_player=True)
                else:
                    self._show_right_meeting(meeting)
                if not self._updating_rows_selection:
                    self.split.set_show_content(True)
                if was_recording and not self._updating_rows_selection:
                    update_live = getattr(self, "_update_live", None)
                    if update_live is not None:
                        update_live()
            elif (
                self.recording_page is not None
                and not self._external_meeting
                and self.recording_page.meeting.key == meeting.key
                and meeting.lifecycle in ("recording", "saving")
            ):
                show_rec = getattr(self, "_show_right_recording", None)
                if show_rec is not None:
                    show_rec(meeting)
                if not self._updating_rows_selection:
                    self.split.set_show_content(True)
            elif (
                not self._updating_rows_selection
                and getattr(self, "_show_right_recording", None) is not None
                and LibraryWindow._is_live_detail_ready(self, meeting)
            ):
                self._show_right_recording(meeting)
                self.split.set_show_content(True)
                update_live = getattr(self, "_update_live", None)
                if update_live is not None:
                    update_live()
            else:
                close_rec = getattr(self, "_close_right_recording", None)
                if close_rec is not None:
                    close_rec()
                self._close_right_meeting()
        else:
            self._preview_key = meeting.key
            self.selected = meeting
        self._recording_line(getattr(self.get_application(), "recorder", None))
        self._refresh_export()

    def _connect_player(self):
        if not self.player.loaded:
            self._loaded_key = None
        self.get_application().watch_player(self.player)
        self.player.connect("error", self._playback_error)
        if self.meeting_page is not None:
            self.meeting_page.rebind_player(self.player)

    def _playback_error(self, _player, _message):
        self._playback_failed = True
        self._loaded_key = None
        if self.meeting_page is not None:
            self.meeting_page._error()

    def _play_selected(self):
        if self.recording_page:
            return
        if self.search_page:
            if self.search_page.hit and self._loaded_key == self.search_page.hit.key:
                self.player.toggle()
            else:
                self.search_page.play_from()
            return
        meeting = self.meeting_page.meeting if self.meeting_page else self.selected
        if meeting is None or not meeting.has_audio:
            return
        if self._ensure_player(meeting):
            self.player.toggle()

    def _skip_selected(self, delta_ms):
        if self.recording_page:
            return
        if self.search_page:
            if self.search_page.hit and self._loaded_key == self.search_page.hit.key:
                self.player.skip(delta_ms)
            return
        meeting = self.meeting_page.meeting if self.meeting_page else self.selected
        if meeting is None or not meeting.has_audio:
            return
        if self._ensure_player(meeting):
            self.player.skip(delta_ms)

    RATES = ("0.75", "1", "1.25", "1.5", "1.75", "2")

    def _change_rate(self, action, value):
        if value.get_string() not in self.RATES:
            return
        action.set_state(value)
        self._rate = float(value.get_string())
        self.player.set_rate(self._rate)
        if self.meeting_page:
            self.meeting_page.rate_changed(self._rate)

    def _step_rate(self, direction):
        action = self.lookup_action("rate")
        index = self.RATES.index(action.get_state().get_string())
        index = max(0, min(len(self.RATES) - 1, index + direction))
        action.change_state(GLib.Variant("s", self.RATES[index]))

    def _ensure_player(self, meeting):
        if self.player.loaded and self._loaded_key == meeting.key and self._loaded_directory == meeting.directory:
            return not self._playback_failed
        use_file = meeting.audio_file is not None
        if not meeting.has_audio or (not use_file and not FolderPlayer.can_play(meeting)):
            return False
        desired = Player if use_file else FolderPlayer
        if type(self.player) is not desired:
            self.player.shutdown()
            self.player = desired()
            self.player.set_rate(self._rate)
            self._connect_player()
        self._playback_failed = False
        # A (re)load replaces what the player plays, so listening ends here too.
        self.listen.stop()
        try:
            if isinstance(self.player, FolderPlayer):
                self.player.load_tracks(meeting.tracks, meeting.duration_ms)
            else:
                self.player.load(meeting.audio_file.as_uri(), meeting.duration_ms)
        except (OSError, ValueError, RuntimeError, GLib.Error) as error:
            self._playback_error(self.player, str(error))
        if not self._playback_failed:
            self._loaded_key = meeting.key
            self._loaded_directory = meeting.directory
        self.player.set_rate(self._rate)
        return not self._playback_failed

    def _stop_playback(self, *, clear_selection=False):
        self.player.stop()
        self.listen.stop()
        self._loaded_key = None
        if clear_selection:
            self._preview_key = None
            self._preview_signature_value = None
        self._playback_failed = False

    def _open_selected(self):
        if self.selected:
            self.open_meeting(self.selected)

    def _close_search(self):
        if not self.search_page:
            return
        page = self.search_page
        self.search_page = None
        self._closing_search = True
        try:
            page.close()
            self.search_sidebar_slot.set_child(None)
            self.search_detail_slot.set_child(None)
            if self.search_entry.get_text():
                self.search_entry.set_text("")
            if id(self.search_entry) in self._focused_editors:
                self._editor_focus(self.search_entry, False)
        finally:
            self._closing_search = False
        self.sidebar_stack.set_visible_child_name("library")
        self.detail_stack.set_visible_child_name(
            "recording"
            if self.recording_page and not self._external_meeting
            else ("meeting" if self.meeting_page else "empty")
        )
        self._stop_playback()
        self._queue_sidebar_scrolled()

    def _enter_search(self, *, focus=True):
        from .search_page import SearchPage

        if self.search_page:
            if hasattr(self, "split") and self.split is not None and self.split.get_collapsed():
                self.split.set_show_content(False)
            if focus:
                self.search_entry.grab_focus()
            return
        if self._external_meeting:
            self._pop_pushed_pages()
        else:
            if self.recording_page:
                self._close_right_recording()
            if self.meeting_page:
                self._close_right_meeting()
        self._stop_playback()
        if self.root_box.get_visible_child_name() != "library":
            self.root_box.set_visible_child_name("library")
        if hasattr(self, "split") and self.split is not None:
            self.split.set_show_content(False)
        self.search_page = SearchPage(self)
        self.search_sidebar_slot.set_child(self.search_page)
        self.search_detail_slot.set_child(self.search_page.content_pane)
        self.sidebar_stack.set_visible_child_name("search")
        self.detail_stack.set_visible_child_name("search")
        self._queue_sidebar_scrolled()
        if focus:
            self.search_entry.grab_focus()
        self._update_live()
        self._refresh_export()

    def _pop_pushed_pages(self):
        self.set_title(text("app.name"))
        was_external = self._external_meeting
        if self.search_page:
            self._close_search()
        if self.recording_page and self._external_meeting:
            self.recording_page.close()
            self.recording_page = None
            self._external_meeting = False
        if self.meeting_page and self._external_meeting:
            self.meeting_page.close()
            self.meeting_page = None
            self._external_meeting = False
            self._stop_playback()
        if self.navigation.get_navigation_stack().get_n_items() > 1:
            self.navigation.pop()
        if was_external:
            self.monitor.refresh()

    def open_meeting(self, meeting, *, external=False):
        if (
            self.recording_page
            and self.recording_page.meeting.key == meeting.key
            and self._external_meeting == external
            and meeting.lifecycle in ("recording", "saving")
        ):
            if not external:
                self.split.set_show_content(True)
            return
        if self._external_meeting or self.search_page:
            self._pop_pushed_pages()
        if meeting.lifecycle in ("recording", "saving"):
            if external:
                self._close_right_recording()
                self._close_right_meeting()
                self._stop_playback()
                self._external_meeting = True
                from .recording_page import RecordingPage

                self.recording_page = RecordingPage(self, meeting)
                self.navigation.push(self.recording_page)
                self._update_live()
                self._refresh_export()
                return
            self._stop_playback()
            self._external_meeting = False
            self._preview_key = meeting.key
            self.selected = meeting
            row = self._row_by_key.get(meeting.key)
            if row is not None and self.rows.get_selected_row() is not row:
                self.rows.handler_block(self._selection_handler)
                self.rows.select_row(row)
                self.rows.handler_unblock(self._selection_handler)
            if self.root_box.get_visible_child_name() != "library":
                self.root_box.set_visible_child_name("library")
            self._show_right_recording(meeting)
            self.split.set_show_content(True)
            self._recording_line(getattr(self.get_application(), "recorder", None))
            self._update_live()
            self._refresh_export()
            return
        if external:
            self._close_right_recording()
            self._close_right_meeting()
            self._stop_playback()
            self._external_meeting = True
            if not self._ensure_player(meeting):
                desired = Player if meeting.audio_file is not None else FolderPlayer
                self.listen.stop()
                self.player.shutdown()
                self.player = desired()
                self.player.set_rate(self._rate)
                self._connect_player()
            self.meeting_page = MeetingPage(self, meeting)
            self.navigation.push(self.meeting_page)
            self._update_live()
            self._refresh_export()
            return
        if self._preview_key != meeting.key or self._loaded_key != meeting.key:
            self._stop_playback()
        self._external_meeting = False
        self._preview_key = meeting.key
        self.selected = meeting
        row = self._row_by_key.get(meeting.key)
        if row is not None and self.rows.get_selected_row() is not row:
            self.rows.handler_block(self._selection_handler)
            self.rows.select_row(row)
            self.rows.handler_unblock(self._selection_handler)
        if self.root_box.get_visible_child_name() != "library":
            self.root_box.set_visible_child_name("library")
        if not self._ensure_player(meeting):
            desired = Player if meeting.audio_file is not None else FolderPlayer
            self.listen.stop()
            self.player.shutdown()
            self.player = desired()
            self.player.set_rate(self._rate)
            self._connect_player()
        self._show_right_meeting(meeting, rebind_player=True, reset_scroll=True)
        self.split.set_show_content(True)
        self._update_live()
        self._refresh_export()

    def toggle_search(self, *_):
        if self.search_page:
            self.back_to_library()
            return
        self._enter_search(focus=True)

    def _back_action(self, *_):
        if (
            self.search_page is not None
            and hasattr(self, "split")
            and self.split is not None
            and self.split.get_collapsed()
            and self.split.get_show_content()
        ):
            self.search_page.show_results()
            return
        self.back_to_library()

    def back_to_library(self, *_):
        had_pushed = bool(self.search_page or self._external_meeting)
        if had_pushed:
            self._pop_pushed_pages()
            if self.snapshot is not None and not self.snapshot.meetings and not LibraryWindow._has_import_cards(self):
                self.root_box.set_visible_child_name("offline" if self.snapshot.unavailable else "empty")
            elif self.root_box.get_visible_child_name() == "library":
                target_row = self.rows.get_selected_row()
                if target_row is not None:
                    self.set_focus(target_row)
                self._updating_rows_selection = True
                try:
                    self._selected(self.rows, target_row)
                finally:
                    self._updating_rows_selection = False
            self._refresh_export()
            return
        if self.recording_page and not self._external_meeting:
            self.set_title(text("app.name"))
            self.rows.handler_block(self._selection_handler)
            self.rows.select_row(None)
            self.rows.handler_unblock(self._selection_handler)
            self.selected = None
            self._close_right_recording()
            self._stop_playback(clear_selection=True)
            self.split.set_show_content(False)
            if self.snapshot is not None and not self.snapshot.meetings and not LibraryWindow._has_import_cards(self):
                self.root_box.set_visible_child_name("offline" if self.snapshot.unavailable else "empty")
            self._recording_line(getattr(self.get_application(), "recorder", None))
            self._refresh_export()
            return
        self.set_title(text("app.name"))
        self.rows.handler_block(self._selection_handler)
        self.rows.select_row(None)
        self.rows.handler_unblock(self._selection_handler)
        self.selected = None
        self._close_right_meeting()
        self._stop_playback(clear_selection=True)
        self.split.set_show_content(False)
        self.monitor.refresh()
        self._refresh_export()

    def _page_popped(self, _navigation, page):
        if page is self.search_page:
            page.close()
            self.search_page = None
            self._stop_playback()
        if page is self.recording_page:
            page.close()
            self.recording_page = None
        if page is self.meeting_page and self._external_meeting:
            page.close()
            self.meeting_page = None
            self._external_meeting = False
            self._stop_playback()
            self.monitor.refresh()
        if not self.search_page and not self.recording_page and not self._external_meeting:
            self.set_title(text("app.name"))
            if self.root_box.get_visible_child_name() == "library":
                self._updating_rows_selection = True
                try:
                    self._selected(self.rows, self.rows.get_selected_row())
                finally:
                    self._updating_rows_selection = False
        self._refresh_export()

    @staticmethod
    def _is_folder_open_path(path):
        return path is not None and (
            path.name == "session.json" or (not path.suffix and path.name.startswith(("Recording-", "Received-")))
        )

    def _ensure_import_executor(self):
        if getattr(self, "_import_executor", None) is None:
            from concurrent.futures import ThreadPoolExecutor

            self._import_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cassini-import")
        return self._import_executor

    def open_files(self, files):
        from .import_dialog import ImportDialog

        files = tuple(files)
        if len(files) == 1:
            return self.open_file(files[0])
        imports = []
        for file in files:
            path = Path(file.get_path()) if file.get_path() else None
            if LibraryWindow._is_folder_open_path(path):
                self.open_file(file)
            else:
                imports.append(str(path) if path else None)
        if imports:
            self._ensure_import_executor()
            ImportDialog(self, tuple(imports))

    def open_file(self, file):
        raw_path = file.get_path()
        path_hint = Path(raw_path) if raw_path else None
        if path_hint is not None and path_hint.suffix.lower() == ".zip":
            from .import_dialog import ImportDialog

            self._ensure_import_executor()
            return ImportDialog(self, (raw_path,))
        from .open_meeting import load_open_meeting
        from .received import import_one

        activity = dict(self.monitor.capture_activity())

        def load(path):
            return load_open_meeting(
                path, self.monitor.scanner.root, scanner=self.monitor.scanner, capture_activity=activity
            )

        if LibraryWindow._is_folder_open_path(path_hint):

            def opened_folder():
                return load(Path(file.get_path())), False

            self._opening_files += 1
            future = self.monitor._executor.submit(opened_folder)
            future.add_done_callback(lambda f: GLib.idle_add(self._opened_file, f, file))
            return

        from threading import Event

        cancel = Event()
        cancels = getattr(self, "_import_cancels", None)
        if cancels is not None:
            cancels.add(cancel)

        def opened_import():
            if not file.get_path():
                raise ValueError("The recording is not available as a local file")
            path = Path(file.get_path())
            if path.is_dir():
                return self.monitor._executor.submit(lambda: (load(path), False)).result()
            meeting, added = import_one(path, cancel=cancel)
            return meeting, True, added

        self._opening_files += 1
        if path_hint is not None:
            self._begin_import_entries(cancel, (raw_path,))
        future = self._ensure_import_executor().submit(opened_import)
        future.add_done_callback(lambda f: GLib.idle_add(self._opened_file, f, file, cancel))

    def _opened_file(self, future, file, cancel=None):
        from concurrent.futures import CancelledError

        self._opening_files -= 1
        if cancel is not None:
            self._finish_import_entries(cancel)
        cancels = getattr(self, "_import_cancels", None)
        if cancels is not None and cancel is not None:
            cancels.discard(cancel)
        if self.monitor._closed:
            return False
        try:
            meeting, imported, *added = future.result()
            if imported:
                if self.meeting_page or self.recording_page or self.search_page:
                    self.back_to_library()
                if added and added[0]:
                    self._fresh_imports.add(meeting.key)
                self._pending_selection = meeting.key
                self.monitor.refresh_now()
            else:
                from .library_location import owned_directory

                try:
                    inside = owned_directory(meeting.directory) == meeting.directory
                except ValueError:
                    inside = False
                watched = inside and meeting.id == meeting_id(meeting.directory)
                if watched:
                    self.monitor.watch_meeting(meeting.directory)
                    self._library_loaded(meeting)
                self.open_meeting(meeting, external=not watched)
        except CancelledError:
            return False
        except (OSError, ValueError) as error:
            # Import failed: retain the Library and show its diagnostic. Opening
            # the readable header in the legacy player would disguise a failed
            # copy, incomplete stream, or full Library as a successful import.
            if cancel is not None and file.get_path():
                LibraryWindow._add_failed_imports(self, (file.get_path(),))
            self.toast(str(error))
        return False

    def _close_after_trash(self):
        app = self.get_application()
        if app is None:
            return
        previous = (getattr(app, "_capture_close_allowed", False), app._closing_background_windows)
        app._capture_close_allowed, app._closing_background_windows = self._trash_close_flags
        try:
            self.close()
        finally:
            app._capture_close_allowed, app._closing_background_windows = previous

    def _close(self, *_args):
        if self.get_application().confirm_capture_close(self):
            return True
        app = self.get_application()
        self._trash_close_flags = (getattr(app, "_capture_close_allowed", False), app._closing_background_windows)
        if app.meeting_trash.flush(self._close_after_trash):
            return True
        self.get_application().window_closing(self)
        if self.recording_details_dialog and not self.recording_details_dialog.closed:
            self.recording_details_dialog.force_close()
        if self.export_dialog and not self.export_dialog.closed:
            self.export_dialog.cancel()
        if self.zip_export_dialog and not self.zip_export_dialog.closed:
            self.zip_export_dialog.cancel()
        if self.rename_dialog and not self.rename_dialog.closed:
            self.rename_dialog.force_close()
        if self.recording_page:
            self.recording_page.close()
            self.recording_page = None
        if self.meeting_page:
            self.meeting_page.close()
            self.meeting_page = None
        if self._stars is not None:
            self._stars.close()
        if self._activity_timer:
            GLib.source_remove(self._activity_timer)
            self._activity_timer = 0
        if self._drop_error_timer:
            GLib.source_remove(self._drop_error_timer)
            self._drop_error_timer = 0
        if self._sidebar_scroll_source:
            GLib.source_remove(self._sidebar_scroll_source)
            self._sidebar_scroll_source = 0
        self._drag_active = False
        self._reset_drag_session()
        self._cancel_drop_validation()
        if self._activity_source is not None:
            self._activity_source.close()
        if self.search_page:
            self._close_search()
        self.spectrograms.close()
        for owner, handler in self._spectro_handlers:
            owner.disconnect(handler)
        self._spectro_handlers.clear()
        self.listen.stop()
        self.player.shutdown()
        for cancel in tuple(self._import_cancels):
            cancel.set()
        for dialog in tuple(self._import_dialogs):
            dialog.cancel()
        if self._import_executor is not None:
            self._import_executor.shutdown(wait=False, cancel_futures=True)
        self.monitor.close()
        return False
