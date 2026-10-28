# main.py
#
# Copyright 2026 Silvio Tomatis
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
# SPDX-License-Identifier: GPL-3.0-or-later

import os
import sys
from gettext import gettext as _

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gst", "1.0")

from gi.repository import Adw, Gio, GLib, Gst, Gtk  # noqa: E402

from .window import RESOURCE, CassiniWindow  # noqa: E402


class CassiniApplication(Adw.Application):
    """The application: one window per open meeting."""

    def __init__(self, application_id, version, profile):
        super().__init__(
            application_id=application_id,
            flags=Gio.ApplicationFlags.HANDLES_OPEN,
            resource_base_path=RESOURCE,
        )
        self.version = version
        self.profile = profile
        self.settings_schema = "com.gocassini.Viewer"
        self.transcriber = None
        self._transcription_holds = 0
        self._transcription_generation = 0
        self._quitting = False
        self.transcription_enabled = os.environ.get("CASSINI_SCREENSHOT_ISOLATED") != "1"
        self.recorder = None
        self._native_held = False
        self._recording_cookie = 0
        self.recording_error = None
        self.record_first = None
        self.comfort = None
        self.global_shortcut = None
        self._pending_recording = None
        self.set_option_context_parameter_string(_("[FILE…]"))

        self._add_action("open", self.on_open, ["<primary>o"])
        self._add_action("about", self.on_about)
        self._add_action("quit", self.on_quit, ["<primary>q"])
        self._add_action("record", self.on_record, ["<primary>r"])
        self._add_action("browser-meetings", self.on_browser_meetings)
        self._add_action("stop-recording", self.on_stop_recording)
        self._add_action("open-recording", self.on_open_recording)
        self.set_accels_for_action("window.close", ["<primary>w"])
        self.set_accels_for_action("win.play-pause", ["space", "AudioPlay", "AudioPause"])
        self.set_accels_for_action("win.skip-back", ["Left"])
        self.set_accels_for_action("win.skip-forward", ["Right"])
        self.set_accels_for_action("win.back-library", ["<alt>Left"])
        self.set_accels_for_action("win.nudge-forward", ["<alt>Right"])
        self.set_accels_for_action("win.search", ["<primary>f"])
        self.set_accels_for_action("win.next-match", ["<primary>g"])
        self.set_accels_for_action("win.previous-match", ["<primary><shift>g"])
        self.set_accels_for_action("win.show-sidebar", ["F9"])
        self.set_accels_for_action("win.show-properties", ["<primary>i"])
        self.set_accels_for_action("win.follow-along", ["<primary>l"])
        self.set_accels_for_action("win.show-transcript", ["<alt>1"])
        self.set_accels_for_action("win.show-summary", ["<alt>2"])
        self.set_accels_for_action("win.export('txt')", ["<primary>e"])

    def do_startup(self):
        Adw.Application.do_startup(self)
        Gst.init(None)
        from .recording_comfort import RecordingComfort

        self.comfort = RecordingComfort(self, automatic=os.environ.get("CASSINI_SCREENSHOT_ISOLATED") != "1")
        if not os.environ.get("CASSINI_SCREENSHOT_ISOLATED") and not os.environ.get("CASSINI_ISOLATED_AUDIO"):
            from .global_shortcut import GlobalShortcut

            self.global_shortcut = GlobalShortcut(self.on_global_record)

    def _add_action(self, name, callback, accels=None):
        action = Gio.SimpleAction.new(name, None)
        action.connect("activate", callback)
        self.add_action(action)
        if accels:
            self.set_accels_for_action(f"app.{name}", accels)

    def watch_player(self, player):
        """Only one viewer plays at a time; recording devices are independent."""
        if getattr(player, "_cassini_watched", False):
            return
        player._cassini_watched = True

        def playing_changed(active, _property):
            if active.playing:
                for window in self.get_windows():
                    other = getattr(window, "player", None)
                    if other is not None and other is not active and other.playing:
                        other.pause()

        player.connect("notify::playing", playing_changed)

    def new_window(self):
        window = CassiniWindow(application=self)
        if self.profile == "development":
            window.add_css_class("devel")
        return window

    def do_activate(self):
        self._quitting = False
        from .library_window import LibraryWindow

        window = next((w for w in self.get_windows() if isinstance(w, LibraryWindow)), None)
        if window is None:
            window = LibraryWindow(self)
        window.present()

    def do_open(self, files, _n_files, _hint):
        for file in files:
            self.open_in_new_window(file)

    def open_in_new_window(self, file):
        """Open *file* in an empty window if there is one, else in a new window."""
        self.do_activate()
        from .library_window import LibraryWindow

        window = next(w for w in self.get_windows() if isinstance(w, LibraryWindow))
        window.open_file(file)

    def on_open(self, *_args):
        dialog = Gtk.FileDialog(title=_("Open Meeting"))
        meetings = Gtk.FileFilter()
        meetings.set_name(_("Opus Audio"))
        for mime in ("audio/ogg", "audio/x-opus+ogg", "audio/opus"):
            meetings.add_mime_type(mime)
        meetings.add_suffix("opus")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(meetings)
        sessions = Gtk.FileFilter()
        sessions.set_name(_("Recording Session"))
        sessions.add_pattern("session.json")
        filters.append(sessions)
        dialog.set_filters(filters)
        dialog.set_default_filter(meetings)
        dialog.open_multiple(self.get_active_window(), None, self._on_open_chosen)

    def _on_open_chosen(self, dialog, result):
        try:
            files = dialog.open_multiple_finish(result)
        except GLib.Error:
            return
        for i in range(files.get_n_items()):
            self.open_in_new_window(files.get_item(i))

    def confirm_capture_close(self, window, *, quitting=False):
        from .capture_guard import confirm_capture_close

        return confirm_capture_close(self, window, quitting=quitting)

    def on_quit(self, *_args):
        if self.confirm_capture_close(self.get_active_window(), quitting=True):
            return
        from .capture_guard import finish_close

        self.begin_exit()
        finish_close(self, list(self.get_windows()))
        self.quit()

    def on_browser_meetings(self, *_args):
        from .browser_window import BrowserMeetingsWindow

        window = next((w for w in self.get_windows() if isinstance(w, BrowserMeetingsWindow)), None)
        if window is None:
            window = BrowserMeetingsWindow(self)
        window.present()

    @property
    def recording_settings(self):
        return Gio.Settings.new(self.settings_schema)

    def browser_capture_active(self):
        return any(
            (getattr(window, "_activity_source", None) and window._activity_source.snapshot)
            or any(status.get("recording") for status in getattr(window, "_browser_snapshot", {}).get("statuses", []))
            for window in self.get_windows()
        )

    def on_record(self, *_args):
        if self.browser_capture_active():
            self.stop_browser_recordings()
            return
        if self.recorder and (self.recorder.pipeline is not None or self.recorder.starting):
            self.stop_native()
            return
        self.do_activate()
        if self.recording_settings.get_boolean("skip-record-first"):
            self.start_native()
        elif not self.record_first:
            from .record_first import RecordFirst

            self.record_first = RecordFirst(self, self.get_active_window())
            self.record_first.connect("closed", lambda *_: setattr(self, "record_first", None))

    def on_global_record(self, *_):
        active = self.recorder and (self.recorder.pipeline is not None or self.recorder.starting)
        active = active or self.browser_capture_active()
        if (
            not active
            and self.recording_settings.get_boolean("skip-record-first")
            and self.get_windows()
            and not any(w.get_visible() for w in self.get_windows())
        ):
            self.start_native()
        else:
            self.on_record()

    def start_native(self, *, test=False):
        from .library import library_root
        from .native_recorder import NativeRecorder

        if self.recorder is None:
            self.recorder = NativeRecorder()
            self.recorder.connect("started", self._native_started)
            self.recorder.connect("finished", self._native_finished)
            self.recorder.connect("error", self._native_error)
            self.recorder.connect("changed", self._native_changed)
        if self.recorder.pipeline is not None or self.recorder.starting:
            return
        self.recording_error = None
        for window in self.get_windows():
            player = getattr(window, "player", None)
            if player is not None and player.playing:
                player.pause()
        self.hold()
        self._native_held = True
        self.recorder.start(library_root(), test=test)

    def _native_started(self, _recorder, path):
        self._pending_recording = path
        self._recording_cookie = self.inhibit(
            self.get_active_window(), Gtk.ApplicationInhibitFlags.SUSPEND, _("Recording a meeting")
        )
        self._native_changed()

    def _native_changed(self, *_):
        if self.comfort:
            self.comfort.native_level()
        for window in self.get_windows():
            if hasattr(window, "capture_changed"):
                window.capture_changed()

    def _release_native(self):
        if self._recording_cookie:
            self.uninhibit(self._recording_cookie)
            self._recording_cookie = 0
        if self._native_held:
            self._native_held = False
            self.release()

    def _native_finished(self, _recorder, _path):
        self._pending_recording = None
        if (
            self.transcription_enabled
            and not self._quitting
            and _recorder.session is not None
            and any(track["frames"] > 0 for track in _recorder.session.manifest["tracks"])
        ):
            self.write_down(_path)
        self._release_native()
        self._native_changed()

    def _native_error(self, _recorder, message):
        self.recording_error = message
        if not self.recorder.pipeline and not self.recorder.starting:
            self._release_native()
        self._native_changed()

    def stop_native(self, *_):
        if self.recorder:
            self.recorder.stop()

    def stop_browser_recordings(self, directory=None):
        from concurrent.futures import ThreadPoolExecutor

        from .capture_guard import stop_browser_captures

        executor = ThreadPoolExecutor(max_workers=1)
        future = executor.submit(stop_browser_captures, directory)

        def done(result):
            executor.shutdown(wait=False)

            def delivered():
                try:
                    paths = result.result()
                    if self.transcription_enabled:
                        for path in paths:
                            self.write_down(path)
                except (OSError, ValueError) as error:
                    self.recording_error = str(error)
                self._native_changed()
                return False

            GLib.idle_add(delivered)

        future.add_done_callback(done)

    def write_down(self, meeting, *, resume=False):
        if self._quitting:
            return
        from .transcription import TranscriptionQueue

        if self.transcriber is None:
            generation = self._transcription_generation

            def changed(_directory):
                GLib.idle_add(self._transcription_changed)

            def finished(_directory):
                GLib.idle_add(self._transcription_finished, generation)

            self.transcriber = TranscriptionQueue(changed, finished)
        directory = getattr(meeting, "directory", meeting)
        if self.transcriber.enqueue(directory, resume=resume):
            self._transcription_holds += 1
            self.hold()

    def _transcription_changed(self):
        for window in self.get_windows():
            if hasattr(window, "monitor"):
                window.monitor.refresh()
        return False

    def _transcription_finished(self, generation):
        if generation != self._transcription_generation:
            return False
        self._transcription_changed()
        if self._transcription_holds:
            self._transcription_holds -= 1
            self.release()
        return False

    def observe_library(self, snapshot, previous):
        if not self.transcription_enabled:
            return
        from .transcription import should_auto_write

        for meeting in snapshot.meetings:
            if (
                (previous is None or meeting.key not in previous)
                and meeting.lifecycle == "saved"
                and meeting.transcript in ("queued", "running")
            ):
                self.write_down(meeting, resume=True)
            elif should_auto_write(meeting, previous):
                self.write_down(meeting)

    def observe_browser_recordings(self, snapshot, previous):
        if not self.transcription_enabled or previous is None:
            return
        for recording in snapshot.get("recordings", []):
            path = recording.get("path")
            if isinstance(path, str) and path not in previous:
                self.write_down(path)

    def on_stop_recording(self, *_):
        if self.recorder and (self.recorder.pipeline is not None or self.recorder.starting):
            self.stop_native()
        if self.browser_capture_active():
            self.stop_browser_recordings()

    def on_open_recording(self, *_):
        self.do_activate()
        window = self.get_active_window()
        if hasattr(window, "open_live_recording"):
            window.open_live_recording()
        if self.comfort:
            self.comfort.tick()

    def begin_exit(self):
        self._quitting = True
        self._transcription_generation += 1
        if getattr(self, "_capture_background", False):
            self._capture_background = False
            self.release()
        if self.transcriber:
            self.transcriber.close()
            self.transcriber = None
        while self._transcription_holds:
            self._transcription_holds -= 1
            self.release()

    def window_closing(self, window):
        if not self._capture_background_active() and not any(w is not window for w in self.get_windows()):
            self.begin_exit()

    def _capture_background_active(self):
        native = self.recorder and (self.recorder.pipeline is not None or self.recorder.starting)
        browser = any(
            getattr(window, "_activity_source", None) and window._activity_source.snapshot
            for window in self.get_windows()
        )
        return getattr(self, "_capture_background", False) and not self._quitting and bool(native or browser)

    def do_shutdown(self):
        if self.global_shortcut:
            self.global_shortcut.close()
        if self.comfort:
            self.comfort.close()
        self.begin_exit()
        Adw.Application.do_shutdown(self)

    def on_about(self, *_args):
        about = Adw.AboutDialog(
            application_name=_("Cassini"),
            application_icon=self.get_application_id(),
            developer_name="Codemyriad",
            version=self.version,
            website="https://github.com/codemyriad/cassini-gnome",
            issue_url="https://github.com/codemyriad/cassini-gnome/issues",
            support_url="https://format.gocassini.com/",
            developers=["Silvio Tomatis https://github.com/silviot"],
            copyright="© 2026 Silvio Tomatis",
            license_type=Gtk.License.GPL_3_0,
            comments=_("Play meeting recordings and read along with the transcript stored inside them."),
        )
        # Translators: Replace "translator-credits" with your name/username, and optionally an email or URL.
        about.set_translator_credits(_("translator-credits"))
        about.add_link(_("Cassini Portable Meeting Format"), "https://format.gocassini.com/")
        about.present(self.get_active_window())


def main(application_id, version, profile):
    """The application's entry point."""
    # Otherwise --help says "Usage: python3".
    GLib.set_prgname("cassini-viewer")
    app = CassiniApplication(application_id, version, profile)
    return app.run(sys.argv)
