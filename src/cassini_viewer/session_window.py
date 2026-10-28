# SPDX-License-Identifier: GPL-3.0-or-later
"""Play the independent sources in a browser recording session."""

from gettext import gettext as _
from pathlib import Path

from gi.repository import Adw, Gio, Gtk

from .player import Player
from .recording_session import load_session
from .transcript import format_time


class SessionWindow(Adw.ApplicationWindow):
    def __init__(self, application, file):
        super().__init__(application=application, title=_("Recorded Meeting"), default_width=640, default_height=500)
        self.player = Player()
        application.watch_player(self.player)
        self.path = Path(file.get_path()) if file.get_path() else None
        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=16,
            margin_top=24,
            margin_bottom=24,
            margin_start=24,
            margin_end=24,
        )
        self.status = Gtk.Label(wrap=True, xalign=0)
        box.append(self.status)
        self.tracks = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE, css_classes=["boxed-list"])
        scroller = Gtk.ScrolledWindow(vexpand=True, child=self.tracks)
        box.append(scroller)
        self.time = Gtk.Label(label="0:00")
        box.append(self.time)
        self.scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 1, 1)
        self.scale.set_draw_value(False)
        self.scale.connect("change-value", self._seek)
        box.append(self.scale)
        self.play = Gtk.Button(label=_("Play"), sensitive=False)
        self.play.connect("clicked", lambda *_args: self.player.toggle())
        box.append(self.play)
        folder = Gtk.Button(label=_("Show Recording Files"))
        folder.connect("clicked", lambda *_args: Gtk.FileLauncher.new(file).open_containing_folder(self, None, None))
        box.append(folder)
        view.set_content(box)
        self.set_content(view)
        self.player.connect("notify::position", self._position)
        self.player.connect(
            "notify::playing", lambda *_args: self.play.set_label(_("Pause") if self.player.playing else _("Play"))
        )
        self.player.connect("error", lambda _p, message: self.status.set_label(message))
        self.connect("close-request", self._close)
        try:
            if self.path is None:
                raise ValueError(_("Choose a local recording session."))
            self.manifest = load_session(self.path)
        except (OSError, ValueError, EOFError) as error:
            self.status.set_label(_("Could not open recording: {error}").format(error=error))
            return
        self.set_title(str(self.manifest.get("title", _("Recorded Meeting"))))
        if self.manifest.get("state") != "completed" and not self.manifest.get("problem"):
            self.manifest["problem"] = _("This recording was interrupted. Available audio can still be played.")
        self.status.set_label(
            str(
                self.manifest.get("problem")
                or _("Select a source to listen. Source labels are not verified participant names.")
            )
        )
        for track in self.manifest["tracks"]:
            offset = (track.get("startOffsetMs") or 0) / 1000
            row = Adw.ActionRow(
                title=str(track.get("label", track.get("kind", _("Audio")))),
                use_markup=False,
                subtitle=_("{kind} · starts at {offset:.2f} s · {channels} channel(s)").format(
                    kind=track.get("kind", "audio"), offset=offset, channels=track["channels"]
                ),
            )
            row.track = track
            self.tracks.append(row)
        self.tracks.connect("row-selected", self._selected)
        if self.manifest["tracks"]:
            self.tracks.select_row(self.tracks.get_row_at_index(0))

    def _selected(self, _list, row):
        if row is None:
            return
        track = row.track
        duration = track["frames"] * 1000 // track["sampleRate"]
        self.player.load(Gio.File.new_for_path(str(self.path.parent / track["file"])).get_uri(), duration)
        self.scale.set_range(0, max(1, duration))
        self.play.set_sensitive(duration > 0)
        self._position()

    def _position(self, *_args):
        self.scale.set_value(self.player.position)
        self.time.set_label(f"{format_time(self.player.position)} / {format_time(self.player.duration)}")

    def _seek(self, _scale, _scroll, value):
        self.player.seek(int(value))
        return True

    def _close(self, *_args):
        if self.get_application().confirm_capture_close(self):
            return True
        self.get_application().window_closing(self)
        self.player.shutdown()
        return False
