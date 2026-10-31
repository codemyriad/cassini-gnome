# SPDX-License-Identifier: GPL-3.0-or-later
"""UX17 native Saved voices page; private-store IO uses the Settings worker."""

import logging
from concurrent.futures import CancelledError

from gi.repository import Adw, GLib, Gtk

from .strings import plural, text
from .ux_formats import confirmed_speech
from .voice_store import sample_window
from .voices_logic import stale

LOG = logging.getLogger(__name__)


class SavedVoicesPage(Adw.NavigationPage):
    def __init__(self, settings):
        super().__init__(title=text("common.savedVoices"))
        self.settings, self.app = settings, settings.app
        self.busy, self.closed, self.retry = False, False, None
        self.rows, self.samples, self.player = {}, {}, None
        self._loading = False
        self._samples_again = False
        self._sample_pending = None
        self._signature = None
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        toolbar.add_top_bar(header)
        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup()
        line = Gtk.Box(spacing=12)
        line.append(Gtk.Image(icon_name="security-high-symbolic", css_classes=["success"], valign=Gtk.Align.START))
        line.append(Gtk.Label(label=text("savedVoices.privacy"), wrap=True, xalign=0, hexpand=True))
        group.add(line)
        page.add(group)
        self.people = Adw.PreferencesGroup()
        page.add(self.people)
        self.newer = Gtk.Label(label=text("savedVoices.newerVersion"), wrap=True, xalign=0, visible=False)
        self.people.add(self.newer)
        self.empty = Gtk.Label(label=text("savedVoices.empty"), wrap=True, xalign=0, css_classes=["dim-label"])
        self.people.add(self.empty)
        self.error_group = Adw.PreferencesGroup()
        self.error = Adw.ActionRow(title=text("savedVoices.changeFailed"), use_markup=False, visible=False)
        self.again = Gtk.Button(label=text("common.tryAgain"), valign=Gtk.Align.CENTER)
        self.again.connect("clicked", lambda *_: self._submit(self.retry) if self.retry else None)
        self.error.add_suffix(self.again)
        self.error_group.add(self.error)
        page.add(self.error_group)
        footer = Adw.PreferencesGroup(description=text("savedVoices.howToAdd"))
        self.everyone = Gtk.Button(label=text("savedVoices.forgetEveryone"), halign=Gtk.Align.START, margin_top=16)
        self.everyone.connect("clicked", lambda *_: self.confirm(None))
        footer.add(self.everyone)
        page.add(footer)
        toolbar.set_content(page)
        self.set_child(toolbar)
        self.connect("hidden", lambda *_: self._stop_sample())
        self.timer = GLib.timeout_add(50, self._tick)

    def refresh(self):
        if self.closed:
            return
        state = self.app.voice_store.snapshot()
        signature = (
            state["voices"],
            state["readOnly"],
            self.busy,
            tuple(
                (m.directory, m.has_audio, m.tracks)
                for w in self.app.get_windows()
                if hasattr(w, "snapshot")
                for m in w.snapshot.meetings
            ),
        )
        self.newer.set_visible(state["readOnly"])
        self.empty.set_visible(not state["voices"])
        self.everyone.set_sensitive(bool(state["voices"]) and not state["readOnly"] and not self.busy)
        if signature != self._signature:
            self._signature = signature
            for row, _play, _forget in self.rows.values():
                self.people.remove(row)
            self.rows = {}
            for voice in state["voices"]:
                row = Adw.ActionRow(title=voice["name"], use_markup=False)
                caption = text(
                    "savedVoices.row.meta",
                    meetingCount=plural("savedVoices.row.meetingCount", len(voice["meetings"])),
                    confirmedSpeech=confirmed_speech(voice["seconds"] * 1000),
                )
                if stale(voice):
                    caption += "\n" + text("savedVoices.row.stale")
                row.set_subtitle(caption)
                avatar = Adw.Avatar(size=40, text=voice["name"], show_initials=True)
                row.add_prefix(avatar)
                play = Gtk.Button(
                    icon_name="media-playback-start-symbolic",
                    css_classes=["circular", "flat"],
                    valign=Gtk.Align.CENTER,
                    tooltip_text=text("savedVoices.row.play", name=voice["name"]),
                )
                play.update_property([Gtk.AccessibleProperty.LABEL], [text("savedVoices.row.play", name=voice["name"])])
                play.connect("clicked", lambda _button, identifier=voice["id"]: self._play(identifier))
                row.add_suffix(play)
                forget = Gtk.Button(
                    label=text("savedVoices.row.forget"),
                    valign=Gtk.Align.CENTER,
                    sensitive=not state["readOnly"] and not self.busy,
                )
                forget.connect("clicked", lambda _button, v=voice: self.confirm(v))
                row.add_suffix(forget)
                self.people.add(row)
                self.rows[voice["id"]] = row, play, forget
            self._load_samples(state["voices"])
        self._tick()

    def confirm(self, voice):
        prefix = "confirm.deleteVoiceProfile" if voice else "confirm.forgetEveryone"
        values = dict(name=voice["name"]) if voice else {}
        dialog = Adw.AlertDialog(heading=text(prefix + ".title", **values), body=text(prefix + ".body", **values))
        dialog.add_response("cancel", text("common.cancel"))
        dialog.add_response("forget", text("savedVoices.row.forget") if voice else text(prefix + ".primary"))
        dialog.set_response_appearance("forget", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        operation = (
            (lambda: self.app.voice_store.forget(voice["id"])) if voice else self.app.voice_store.forget_everyone
        )
        dialog.connect("response", lambda _dialog, response: self._submit(operation) if response == "forget" else None)
        dialog.present(self.settings)
        self.confirmation = dialog

    def _submit(self, operation):
        if self.busy or self.closed or self.settings.closed or self.app.voice_store.snapshot()["readOnly"]:
            return
        self._stop_sample()
        self.busy = True
        self.error.set_visible(False)
        self.refresh()
        future = self.settings.executor.submit(operation)
        future.add_done_callback(lambda f: GLib.idle_add(self._finished, f, operation))

    def _finished(self, future, operation):
        self.busy = False
        try:
            future.result()
        except CancelledError:
            return False
        except (OSError, ValueError):
            LOG.exception("Could not change saved voices")
            if not self.closed:
                self.retry = operation
                self.error.set_visible(True)
        else:
            self.retry = None
        if not self.closed:
            self.refresh()
            self.settings.render_voices()
        return False

    def _load_samples(self, voices):
        if self._loading:
            self._samples_again = True
            return
        if self.settings.closed:
            return
        self._loading = True
        # Capture the immutable scanner snapshots on GTK, then read only the
        # private cache on the worker. No Library or voice file IO on GTK.
        meetings = {
            m.directory.name: m for w in self.app.get_windows() if hasattr(w, "snapshot") for m in w.snapshot.meetings
        }

        def load():
            result = {}
            for voice in voices:
                available = sorted(
                    (meetings[k] for k in voice["meetings"] if k in meetings),
                    key=lambda m: m.started_at_ms or 0,
                    reverse=True,
                )
                for meeting in available:
                    if not meeting.has_audio:
                        continue
                    try:
                        cache = self.app.voice_store.meeting(meeting.directory)
                    except (OSError, ValueError):
                        continue
                    selected = sample_window(cache, voice["id"])
                    if selected:
                        result[voice["id"]] = meeting, *selected
                        break
            return result

        future = self.settings.executor.submit(load)
        future.add_done_callback(lambda f: GLib.idle_add(self._samples_ready, f))

    def _samples_ready(self, future):
        self._loading = False
        try:
            samples = future.result()
        except (OSError, ValueError, CancelledError):
            samples = {}
        if not self.closed:
            self.samples = {
                key: value for key, value in samples.items() if not self.app.meeting_trash.hides(value[0].directory)
            }
            self._tick()
            if self._samples_again:
                self._samples_again = False
                self._load_samples(self.app.voice_store.snapshot()["voices"])
        return False

    def _play(self, identifier):
        if identifier not in self.samples or self.busy:
            return
        self._stop_sample()
        meeting, start, end = self.samples[identifier]
        self._sample_meeting = meeting.directory
        from .folder_player import FolderPlayer
        from .player import Player

        # Pause Library playback so samples cannot play on top of a meeting.
        for window in self.app.get_windows():
            if hasattr(window, "player"):
                window.player.pause()
        try:
            if meeting.origin == "own":
                if not FolderPlayer.can_play(meeting):
                    return
                self.player = FolderPlayer()
            else:
                self.player = Player()
            player = self.player
            player.connect("error", lambda *_: self._stop_sample())
            if meeting.origin == "own":
                player.load_tracks(meeting.tracks, meeting.duration_ms)
            else:
                player.load(meeting.audio_file.as_uri(), meeting.duration_ms)
            if self.player is player:
                self._sample_pending = start, end
        except (OSError, ValueError, RuntimeError):
            LOG.exception("Saved voice sample unavailable")
            self._stop_sample()

    def _tick(self):
        if self.closed:
            return GLib.SOURCE_REMOVE
        for identifier, (_row, play, _forget) in self.rows.items():
            play.set_sensitive(identifier in self.samples and not self.busy)
        if self.player is not None and self._sample_pending and self.player._prerolled:
            start, end = self._sample_pending
            self._sample_pending = None
            if not self.player.play_range(start, end):
                self._stop_sample()
        if self.player is not None and self._sample_pending is None and not self.player.playing:
            self._stop_sample()
        return GLib.SOURCE_CONTINUE

    def discard_meeting(self, directory):
        if getattr(self, "_sample_meeting", None) == directory:
            self._stop_sample()
        self.samples = {key: value for key, value in self.samples.items() if value[0].directory != directory}

    def _stop_sample(self):
        self._sample_meeting = None
        self._sample_pending = None
        if self.player:
            self.player.shutdown()
            self.player = None

    def shutdown(self):
        self.closed = True
        self._stop_sample()
        if self.timer:
            GLib.source_remove(self.timer)
            self.timer = 0
