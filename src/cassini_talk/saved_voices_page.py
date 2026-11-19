# SPDX-License-Identifier: GPL-3.0-or-later
"""UX17 / D-977 Saved voices page; private-store IO uses the Settings worker.

Wave 6 (PARITY §5, HARMONY §§10, 12, D98) with L-63 (D106 §5): amber stale
notice above the list, grey people-count heading with flat "+ Add a voice…",
top-aligned rows ("31 meetings · 4 h 12 min", amber "Needs naming again" pill,
play button, Options menu), caption under the list, Forget everyone… apart,
centred empty state with primary "Add a voice…", and a one-line shield privacy
footnote at the bottom. Nothing here touches the network.
"""

import logging
from concurrent.futures import CancelledError

from gi.repository import Adw, Gio, GLib, Gtk

from .i18n import join_list
from .speaker_edits import name_value
from .strings import plural, text
from .tokens import saved_voice_slot
from .ux_formats import meeting_duration
from .voice_store import sample_window
from .voices_logic import stale

LOG = logging.getLogger(__name__)


def stale_notice_text(names):
    """Title and body of the D106 §5 amber stale-voices notice; names use the locale list formatter."""
    return (
        plural("savedVoices.stale.title", len(names)),
        text("savedVoices.stale.body", name=join_list(names)),
    )


def voice_row_meta(voice):
    """D106 §5 (L-63): '31 meetings · 4 h 12 min', without 'confirmed speech'."""
    return text(
        "savedVoices.row.meta",
        meetingCount=plural("savedVoices.row.meetingCount", len(voice["meetings"])),
        confirmedSpeech=meeting_duration(voice["seconds"] * 1000),
    )


class SavedVoicesPage(Adw.NavigationPage):
    def __init__(self, settings):
        from .first_run_hero import LineGlyph

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
        self.newer_group = Adw.PreferencesGroup(visible=False)
        self.newer = Gtk.Label(label=text("savedVoices.newerVersion"), wrap=True, xalign=0, visible=False)
        self.newer_group.add(self.newer)
        page.add(self.newer_group)
        # D106 §5 (L-63): one amber notice above the list when any voice is stale.
        self.stale_group = Adw.PreferencesGroup(visible=False)
        self.stale_notice = Gtk.Box(spacing=10, css_classes=["saved-voices-stale-notice"])
        self.stale_notice.append(
            Gtk.Image(
                icon_name="dialog-warning-symbolic",
                valign=Gtk.Align.START,
                accessible_role=Gtk.AccessibleRole.PRESENTATION,
                css_classes=["saved-voices-stale-icon"],
            )
        )
        stale_text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, hexpand=True)
        self.stale_title = Gtk.Label(xalign=0, wrap=True, css_classes=["saved-voices-stale-title"])
        self.stale_body = Gtk.Label(xalign=0, wrap=True, css_classes=["saved-voices-stale-body"])
        stale_text.append(self.stale_title)
        stale_text.append(self.stale_body)
        self.stale_notice.append(stale_text)
        self.stale_group.add(self.stale_notice)
        page.add(self.stale_group)
        # Grey list label with a flat "+ Add a voice…" button on its right.
        self.add_voice = Gtk.Button(
            child=Adw.ButtonContent(icon_name="list-add-symbolic", label=text("voices.enroll.addAction")),
            valign=Gtk.Align.CENTER,
            css_classes=["flat", "saved-voices-add"],
        )
        self.add_voice.connect("clicked", lambda *_: self.enroll())
        self.people = Adw.PreferencesGroup(header_suffix=self.add_voice, css_classes=["saved-voices-list-group"])
        page.add(self.people)
        self.caption_group = Adw.PreferencesGroup(css_classes=["saved-voices-caption-group"])
        self.caption = Gtk.Label(
            label=text("savedVoices.caption"),
            wrap=True,
            xalign=0,
            css_classes=["dim-label", "saved-voices-caption"],
        )
        self.caption_group.add(self.caption)
        self.models_needed = Gtk.Label(
            label=text("speakers.rememberVoice.modelsNeeded"),
            wrap=True,
            xalign=0,
            margin_top=6,
            visible=False,
            css_classes=["dim-label", "caption"],
        )
        self.caption_group.add(self.models_needed)
        page.add(self.caption_group)
        self.error_group = Adw.PreferencesGroup(visible=False)
        self.error = Adw.ActionRow(
            title=text("savedVoices.changeFailed"), use_markup=False, visible=False, css_classes=["error-line"]
        )
        self.again = Gtk.Button(label=text("common.tryAgain"), valign=Gtk.Align.CENTER)
        self.again.connect("clicked", lambda *_: self._submit(self.retry) if self.retry else None)
        self.error.add_suffix(self.again)
        self.error_group.add(self.error)
        page.add(self.error_group)
        # Forget everyone… sits apart under the list, never beside the privacy footnote.
        self.forget_group = Adw.PreferencesGroup()
        self.everyone = Gtk.Button(
            label=text("savedVoices.forgetEveryone"),
            halign=Gtk.Align.START,
            css_classes=["saved-voices-forget-all"],
        )
        self.everyone.connect("clicked", lambda *_: self.confirm(None))
        self.forget_group.add(self.everyone)
        page.add(self.forget_group)
        # Empty state: centred larger title, body, primary "Add a voice…", no large icon.
        self.empty_group = Adw.PreferencesGroup(visible=False, css_classes=["saved-voices-empty-group"])
        empty_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=10,
            halign=Gtk.Align.CENTER,
            valign=Gtk.Align.CENTER,
            margin_top=140,
            margin_bottom=32,
        )
        self.empty = Gtk.Label(
            label=text("savedVoices.empty"),
            wrap=True,
            justify=Gtk.Justification.CENTER,
            css_classes=["saved-voices-empty-title"],
        )
        empty_box.append(self.empty)
        self.empty_body = Gtk.Label(
            label=text("savedVoices.empty.body"),
            wrap=True,
            justify=Gtk.Justification.CENTER,
            max_width_chars=44,
            css_classes=["dim-label", "saved-voices-empty-body"],
        )
        empty_box.append(self.empty_body)
        self.empty_add_voice = Gtk.Button(
            label=text("voices.enroll.addAction"),
            halign=Gtk.Align.CENTER,
            margin_top=8,
            css_classes=["suggested-action", "primary-action", "saved-voices-empty-add"],
        )
        self.empty_add_voice.connect("clicked", lambda *_: self.enroll())
        empty_box.append(self.empty_add_voice)
        self.empty_models_needed = Gtk.Label(
            label=text("speakers.rememberVoice.modelsNeeded"),
            wrap=True,
            justify=Gtk.Justification.CENTER,
            max_width_chars=44,
            margin_top=4,
            visible=False,
            css_classes=["dim-label", "caption"],
        )
        empty_box.append(self.empty_models_needed)
        self.empty_group.add(empty_box)
        page.add(self.empty_group)
        # Bottom privacy footnote: one centred line with the shield icon, same with voices and empty.
        self.privacy_group = Gtk.Box(
            spacing=6,
            halign=Gtk.Align.CENTER,
            margin_top=10,
            margin_bottom=16,
            margin_start=24,
            margin_end=24,
            css_classes=["saved-voices-privacy"],
        )
        shield = LineGlyph("shield", 14, valign=Gtk.Align.CENTER, css_classes=["saved-voices-shield"])
        self.privacy_group.append(shield)
        self.footnote = Gtk.Label(
            label=text("savedVoices.footnote.computer"),
            wrap=True,
            justify=Gtk.Justification.CENTER,
            css_classes=["dim-label", "saved-voices-footnote"],
        )
        self.privacy_group.append(self.footnote)
        toolbar.set_bottom_bar_style(Adw.ToolbarStyle.FLAT)
        toolbar.add_bottom_bar(self.privacy_group)
        self.enroll_dialog = None
        self.rename_dialog = None
        toolbar.set_content(page)
        self.set_child(toolbar)
        self.connect("hidden", lambda *_: self._stop_sample())
        self.timer = GLib.timeout_add(50, self._tick)

    def can_enroll(self):
        return self.app.recognition_ready()

    def refresh(self):
        if self.closed:
            return
        state = self.app.voice_store.snapshot()
        voices = state["voices"]
        signature = (
            voices,
            state["readOnly"],
            self.busy,
            tuple(
                (m.directory, m.has_audio, m.tracks)
                for w in self.app.get_windows()
                if hasattr(w, "snapshot")
                for m in w.snapshot.meetings
            ),
        )
        has_voices = bool(voices)
        writable = not state["readOnly"] and not self.busy
        ready = self.can_enroll()
        self.newer_group.set_visible(state["readOnly"])
        self.newer.set_visible(state["readOnly"])
        stale_names = [voice["name"] for voice in voices if stale(voice)]
        self.stale_group.set_visible(bool(stale_names))
        if stale_names:
            title, body = stale_notice_text(stale_names)
            self.stale_title.set_label(title)
            self.stale_body.set_label(body)
        self.people.set_visible(has_voices)
        if has_voices:
            self.people.set_title(plural("savedVoices.peopleCount", len(voices)))
        self.caption_group.set_visible(has_voices)
        self.forget_group.set_visible(has_voices)
        self.empty_group.set_visible(not has_voices)
        self.empty.set_visible(not has_voices)
        self.everyone.set_sensitive(has_voices and writable)
        self.add_voice.set_sensitive(ready and writable)
        self.empty_add_voice.set_sensitive(ready and writable)
        self.models_needed.set_visible(has_voices and not ready)
        self.empty_models_needed.set_visible(not has_voices and not ready)
        if signature != self._signature:
            self._signature = signature
            for row in self.rows.values():
                self.people.remove(row.row)
            self.rows = {}
            for voice in voices:
                row = VoiceRow(self, voice, writable)
                self.people.add(row.row)
                self.rows[voice["id"]] = row
            self._load_samples(voices)
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
        self.error_group.set_visible(False)
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
                self.error_group.set_visible(True)
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
            row.set_sample_available(identifier in self.samples and not self.busy)
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
    """One saved voice (L-63, D106 §5): save-order colour (L-41), name, meta, amber stale pill, Play, Options menu."""

    def __init__(self, page, voice, writable):
        self.voice = voice
        name = voice["name"]
        self.row = Adw.PreferencesRow(title=name, use_markup=False, css_classes=["saved-voice-row"])
        box = Gtk.Box(spacing=12, margin_top=12, margin_bottom=12, margin_start=14, margin_end=14)
        self.avatar = Gtk.Label(
            label=initials(name),
            valign=Gtk.Align.START,
            css_classes=["speaker-avatar", f"speaker-avatar-{saved_voice_slot(voice)}"],
        )
        box.append(self.avatar)
        lines = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True, valign=Gtk.Align.START)
        self.name = Gtk.Label(label=name, xalign=0, wrap=True, css_classes=["saved-voice-name"])
        lines.append(self.name)
        self.meta = Gtk.Label(
            label=voice_row_meta(voice),
            xalign=0,
            wrap=True,
            css_classes=["dim-label", "saved-voice-meta"],
        )
        lines.append(self.meta)
        self.stale = stale(voice)
        self.stale_pill = Gtk.Label(
            label=text("savedVoices.row.needsNaming"),
            halign=Gtk.Align.START,
            visible=self.stale,
            css_classes=["saved-voice-stale-pill"],
        )
        lines.append(self.stale_pill)
        box.append(lines)
        self.play = Gtk.Button(css_classes=["circular", "saved-voice-play"], valign=Gtk.Align.START)
        self.play.connect("clicked", lambda *_: page._toggle_sample(voice["id"]))
        self._playing = None
        self.show_playing(False)
        box.append(self.play)
        self.actions = Gio.SimpleActionGroup()
        for action_name, callback, enabled in (
            ("play", lambda: page._play(voice["id"]), False),
            ("rename", lambda: page.rename(voice), writable),
            ("forget", lambda: page.confirm(voice), writable),
        ):
            action = Gio.SimpleAction(name=action_name, enabled=enabled)
            action.connect("activate", lambda _action, _value, run=callback: run())
            self.actions.add_action(action)
        self.row.insert_action_group("voice", self.actions)
        self.menu = Gio.Menu()
        self.menu.append(text("savedVoices.row.playSampleMenu"), "voice.play")
        self.menu.append(text("savedVoices.row.rename"), "voice.rename")
        self.menu.append(text("savedVoices.row.forgetEllipsis"), "voice.forget")
        options = text("savedVoices.row.options.a11y", name=name)
        self.options = Gtk.MenuButton(
            icon_name="view-more-symbolic",
            menu_model=self.menu,
            tooltip_text=options,
            valign=Gtk.Align.START,
            css_classes=["circular", "flat", "saved-voice-options"],
        )
        self.options.update_property([Gtk.AccessibleProperty.LABEL], [options])
        box.append(self.options)
        self.row.set_child(box)

    def set_sample_available(self, available):
        self.play.set_sensitive(available)
        self.actions.lookup_action("play").set_enabled(available)

    def show_playing(self, playing):
        if playing == self._playing:
            return
        self._playing = playing
        label = text("savedVoices.row.stopSample" if playing else "savedVoices.row.play", name=self.voice["name"])
        self.play.set_icon_name("media-playback-stop-symbolic" if playing else "media-playback-start-symbolic")
        self.play.set_tooltip_text(label)
        self.play.update_property([Gtk.AccessibleProperty.LABEL], [label])
