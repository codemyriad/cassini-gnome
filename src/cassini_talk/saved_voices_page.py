# SPDX-License-Identifier: GPL-3.0-or-later
"""UX17 / D-977 Saved voices page; private-store IO uses the Settings worker.

Wave 6 (PARITY §5, HARMONY §§10, 12, D98): one-question voice setup, guided
test-phrase enrollment, rename, delete, sample count and status, sample
playback, and the local-only privacy line. Nothing here touches the network.
"""

import logging
from concurrent.futures import CancelledError

from gi.repository import Adw, GLib, Gtk

from .speaker_edits import name_value
from .strings import plural, text
from .tokens import saved_voice_slot
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
        self._sample_voice = None
        self._sample_meeting = None
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
        self.setup = Adw.PreferencesGroup(visible=False, css_classes=["voice-setup"])
        setup_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.setup_title = Gtk.Label(
            label=text("voices.setup.title"),
            wrap=True,
            xalign=0,
            css_classes=["title-4"],
            accessible_role=Gtk.AccessibleRole.HEADING,
        )
        setup_box.append(self.setup_title)
        setup_box.append(Gtk.Label(label=text("voices.setup.body"), wrap=True, xalign=0))
        setup_box.append(
            Gtk.Label(label=text("voices.setup.privacy.desktop"), wrap=True, xalign=0, css_classes=["dim-label"])
        )
        setup_actions = Gtk.Box(spacing=8, margin_top=4)
        self.setup_enable = Gtk.Button(label=text("voices.setup.enable"), css_classes=["suggested-action"])
        self.setup_enable.connect("clicked", lambda *_: self.answer_setup(True))
        self.setup_later = Gtk.Button(label=text("common.notNow"))
        self.setup_later.connect("clicked", lambda *_: self.answer_setup(False))
        setup_actions.append(self.setup_enable)
        setup_actions.append(self.setup_later)
        setup_box.append(setup_actions)
        self.setup.add(setup_box)
        page.add(self.setup)
        self.people = Adw.PreferencesGroup()
        page.add(self.people)
        self.newer = Gtk.Label(label=text("savedVoices.newerVersion"), wrap=True, xalign=0, visible=False)
        self.people.add(self.newer)
        self.empty = Gtk.Label(label=text("savedVoices.empty"), wrap=True, xalign=0, css_classes=["dim-label"])
        self.people.add(self.empty)
        self.error_group = Adw.PreferencesGroup()
        self.error = Adw.ActionRow(
            title=text("savedVoices.changeFailed"), use_markup=False, visible=False, css_classes=["error-line"]
        )
        self.again = Gtk.Button(label=text("common.tryAgain"), valign=Gtk.Align.CENTER)
        self.again.connect("clicked", lambda *_: self._submit(self.retry) if self.retry else None)
        self.error.add_suffix(self.again)
        self.error_group.add(self.error)
        page.add(self.error_group)
        footer = Adw.PreferencesGroup(description=text("savedVoices.howToAdd"))
        actions = Gtk.Box(spacing=8, margin_top=16)
        self.add_voice = Gtk.Button(label=text("voices.enroll.addAction"))
        self.add_voice.connect("clicked", lambda *_: self.enroll())
        actions.append(self.add_voice)
        self.everyone = Gtk.Button(label=text("savedVoices.forgetEveryone"))
        self.everyone.connect("clicked", lambda *_: self.confirm(None))
        actions.append(self.everyone)
        footer.add(actions)
        self.models_needed = Gtk.Label(
            label=text("speakers.rememberVoice.modelsNeeded"),
            wrap=True,
            xalign=0,
            margin_top=8,
            visible=False,
            css_classes=["dim-label", "caption"],
        )
        footer.add(self.models_needed)
        page.add(footer)
        self.enroll_dialog = None
        self.rename_dialog = None
        toolbar.set_content(page)
        self.set_child(toolbar)
        self.connect("hidden", lambda *_: self._stop_sample())
        self.timer = GLib.timeout_add(50, self._tick)

    def setup_offered(self):
        """`Recognise people you name?` until answered, unless recognition is already On."""
        app = self.app
        if app.diarization_models is None or app.voice_models is None or app.recognition_ready():
            return False
        return app.recording_settings.get_string("voice-setup") == "unanswered"

    def answer_setup(self, enable):
        self.app.answer_voice_setup(enable)
        self.refresh()
        self.settings.render_models()

    def can_enroll(self):
        return self.app.recognition_ready()

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
        writable = not state["readOnly"] and not self.busy
        self.newer.set_visible(state["readOnly"])
        self.empty.set_visible(not state["voices"])
        self.everyone.set_sensitive(bool(state["voices"]) and writable)
        self.setup.set_visible(self.setup_offered())
        ready = self.can_enroll()
        self.add_voice.set_sensitive(ready and writable)
        self.models_needed.set_visible(not ready and not self.setup.get_visible())
        if signature != self._signature:
            self._signature = signature
            for row in self.rows.values():
                self.people.remove(row.row)
            self.rows = {}
            for voice in state["voices"]:
                row = VoiceRow(self, voice, writable)
                self.people.add(row.row)
                self.rows[voice["id"]] = row
            self._load_samples(state["voices"])
        self._tick()

    def enroll(self):
        from .voice_enroll_dialog import VoiceEnrollDialog

        if not self.can_enroll() or self.busy or self.app.voice_store.snapshot()["readOnly"]:
            return
        self._stop_sample()
        self.enroll_dialog = VoiceEnrollDialog(self.settings)
        self.enroll_dialog.present(self.settings)

    def rename(self, voice):
        dialog = Adw.AlertDialog(heading=text("savedVoices.rename.title"))
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        entry = Gtk.Entry(text=voice["name"], max_length=64, activates_default=True)
        entry.update_property([Gtk.AccessibleProperty.LABEL], [text("speakers.nameLabel")])
        blank = Gtk.Label(label=text("savedVoices.rename.blank"), xalign=0, visible=False, css_classes=["error-line"])
        box.append(entry)
        box.append(blank)
        dialog.set_extra_child(box)
        dialog.add_response("cancel", text("common.cancel"))
        dialog.add_response("save", text("savedVoices.rename.save"))
        dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("save")
        dialog.set_close_response("cancel")

        def changed(*_):
            try:
                name = name_value(entry.get_text())
            except ValueError:
                name = None
            blank.set_visible(not name)
            dialog.set_response_enabled("save", bool(name))

        entry.connect("changed", changed)

        def response(_dialog, answer):
            if answer != "save":
                return
            try:
                name = name_value(entry.get_text())
            except ValueError:
                return
            if name and name != voice["name"]:
                self._submit(lambda: self.app.voice_store.rename(voice["id"], name))

        dialog.connect("response", response)
        dialog.entry, dialog.blank = entry, blank
        dialog.present(self.settings)
        self.rename_dialog = dialog

    def confirm(self, voice):
        prefix = "confirm.deleteVoiceProfile" if voice else "confirm.forgetEveryone"
        values = dict(name=voice["name"]) if voice else {}
        dialog = Adw.AlertDialog(heading=text(prefix + ".title", **values), body=text(prefix + ".body", **values))
        dialog.add_response("cancel", text("common.cancel"))
        dialog.add_response("forget", text(prefix + ".primary"))
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
                    key=lambda m: m.sort_time or 0,
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

    def _toggle_sample(self, identifier):
        if self.player is not None and self._sample_voice == identifier:
            self._stop_sample()
        else:
            self._play(identifier)

    def _play(self, identifier):
        if identifier not in self.samples or self.busy:
            return
        self._stop_sample()
        self._sample_voice = identifier
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
        for identifier, row in self.rows.items():
            row.show_playing(self.player is not None and self._sample_voice == identifier)
            row.play.set_sensitive(identifier in self.samples and not self.busy)
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
        self._sample_voice = None
        self._sample_pending = None
        if self.player:
            self.player.shutdown()
            self.player = None

    def shutdown(self):
        self.closed = True
        self._stop_sample()
        if self.enroll_dialog is not None and not self.enroll_dialog.closed:
            self.enroll_dialog.close()
        if self.timer:
            GLib.source_remove(self.timer)
            self.timer = 0


def initials(name):
    words = [part for part in name.strip().split() if part]
    return (words[0][0] + words[-1][0]).upper() if len(words) >= 2 else (words[0][0].upper() if words else "?")


class VoiceRow:
    """One saved voice: fixed save-order colour (L-41), meta, samples, status, Play, Rename, Delete."""

    def __init__(self, page, voice, writable):
        self.voice = voice
        name = voice["name"]
        self.row = Adw.ActionRow(title=name, use_markup=False, css_classes=["saved-voice-row"])
        lines = [
            text(
                "savedVoices.row.meta",
                meetingCount=plural("savedVoices.row.meetingCount", len(voice["meetings"])),
                confirmedSpeech=confirmed_speech(voice["seconds"] * 1000),
            ),
            plural("savedVoices.row.sampleCount", voice["count"]),
        ]
        self.stale = stale(voice)
        if self.stale:
            lines.append(text("savedVoices.row.stale"))
        self.row.set_subtitle("\n".join(lines))
        self.avatar = Gtk.Label(
            label=initials(name),
            valign=Gtk.Align.CENTER,
            css_classes=["speaker-avatar", f"speaker-avatar-{saved_voice_slot(voice)}"],
        )
        self.row.add_prefix(self.avatar)
        self.status = Gtk.Label(
            label=text("savedVoices.row.status.ready"),
            valign=Gtk.Align.CENTER,
            visible=not self.stale,
            css_classes=["saved-voice-ready"],
        )
        self.row.add_suffix(self.status)
        self.play = Gtk.Button(css_classes=["circular", "flat"], valign=Gtk.Align.CENTER)
        self.play.connect("clicked", lambda *_: page._toggle_sample(voice["id"]))
        self._playing = None
        self.show_playing(False)
        self.row.add_suffix(self.play)
        self.rename = Gtk.Button(
            label=text("savedVoices.row.rename"), valign=Gtk.Align.CENTER, sensitive=writable, css_classes=["flat"]
        )
        self.rename.connect("clicked", lambda *_: page.rename(voice))
        self.row.add_suffix(self.rename)
        self.delete = Gtk.Button(label=text("savedVoices.row.delete"), valign=Gtk.Align.CENTER, sensitive=writable)
        self.delete.connect("clicked", lambda *_: page.confirm(voice))
        self.row.add_suffix(self.delete)

    def show_playing(self, playing):
        if playing == self._playing:
            return
        self._playing = playing
        label = text("savedVoices.row.stopSample" if playing else "savedVoices.row.play", name=self.voice["name"])
        self.play.set_icon_name("media-playback-stop-symbolic" if playing else "media-playback-start-symbolic")
        self.play.set_tooltip_text(label)
        self.play.update_property([Gtk.AccessibleProperty.LABEL], [label])
