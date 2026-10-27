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
        self.set_option_context_parameter_string(_("[FILE…]"))

        self._add_action("open", self.on_open, ["<primary>o"])
        self._add_action("about", self.on_about)
        self._add_action("quit", self.on_quit, ["<primary>q"])
        self._add_action("record", self.on_record, ["<primary>r"])
        self._add_action("browser-meetings", self.on_browser_meetings)
        self.set_accels_for_action("window.close", ["<primary>w"])
        self.set_accels_for_action("win.play-pause", ["space", "AudioPlay", "AudioPause"])
        self.set_accels_for_action("win.skip-back", ["<primary>Left"])
        self.set_accels_for_action("win.skip-forward", ["<primary>Right"])
        self.set_accels_for_action("win.nudge-back", ["<alt>Left"])
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
        if file.get_basename() == "session.json":
            from .session_window import SessionWindow

            window = SessionWindow(self, file)
            window.present()
            return
        window = next((w for w in self.get_windows() if isinstance(w, CassiniWindow) and not w.has_meeting), None)
        if window is None:
            window = self.new_window()
        window.open_file(file)
        window.present()

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
        active = self.get_active_window()
        for i in range(files.get_n_items()):
            file = files.get_item(i)
            if i == 0 and isinstance(active, CassiniWindow) and file.get_basename() != "session.json":
                active.open_file(file)
            else:
                self.open_in_new_window(file)

    def on_quit(self, *_args):
        for window in self.get_windows():
            if hasattr(window, "open_result"):
                window.open_result = False
            window.close()

    def on_browser_meetings(self, *_args):
        from .browser_window import BrowserMeetingsWindow

        window = next((w for w in self.get_windows() if isinstance(w, BrowserMeetingsWindow)), None)
        if window is None:
            window = BrowserMeetingsWindow(self)
        window.present()

    def on_record(self, *_args):
        from .recording_window import RecordingWindow

        window = next((w for w in self.get_windows() if isinstance(w, RecordingWindow)), None)
        if window is None:
            window = RecordingWindow(self)
        window.present()

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
