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

import logging
import os
import sys
from concurrent.futures import CancelledError, ThreadPoolExecutor
from gettext import gettext as _
from pathlib import Path
from uuid import uuid4

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
        self.speech_models = None
        self.diarization_models = None
        self.voice_models = None
        self._voice_library_keys = None
        self._voice_matches = frozenset()
        from .voice_store import VoiceStore

        self.voice_store = VoiceStore(changed=lambda: GLib.idle_add(self._voices_changed))
        self._transcription_holds = 0
        self._transcription_generation = 0
        self._quitting = False
        self._capture_background = False
        self._capture_confirmation = None
        self._browser_stops = set()
        self._capture_exiting = False
        self._closing_background_windows = False
        # Screenshot runs keep the "Write it down" actions visible but never
        # start the engine. Otherwise both need the bundle and its runtime.
        screenshot = os.environ.get("CASSINI_SCREENSHOT_ISOLATED") == "1"
        self.writing_unavailable = None if screenshot else self._writing_unavailable()
        self.transcription_enabled = not screenshot and self.writing_unavailable is None
        self.native_captures = {}
        self._last_native = None
        self._browser_snapshot = {}
        self.browser_watch = None
        self._companion_ready = not self.in_flatpak
        self._companion_probe_future = None
        self._companion_probe_timer = 0
        self._companion_probe_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cassini-companion-probe")
        self._browser_seen_saved = None
        self._auto_recording_held = False
        self._auto_recording_pending = None
        self._auto_recording_request = 0
        self._auto_recording_revision = 0
        self._record_start_future = None
        self._source_changes = {}
        self._record_start_group = None
        self._record_start_generation = 0
        self._record_start_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cassini-record-start")
        self._native_held = False
        self._recording_cookie = 0
        self.recording_error = None
        self.record_first = None
        self.settings_dialog = None
        self.library_relocator = None
        self.comfort = None
        self.global_shortcut = None
        self._pending_recording = None
        self.set_option_context_parameter_string(_("[FILE…]"))

        self._add_action("open", self.on_open, ["<primary>o"])
        self._add_action("about", self.on_about)
        self._add_action("preferences", self.on_preferences, ["<primary>comma"])
        self._add_action("quit", self.on_quit, ["<primary>q"])
        self._add_action("record", self.on_record, ["<primary>r"])
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
        self.set_accels_for_action("win.export", ["<primary><shift>e"])

    def do_startup(self):
        Adw.Application.do_startup(self)
        Gst.init(None)
        from .recording_comfort import RecordingComfort

        self.comfort = RecordingComfort(self, automatic=os.environ.get("CASSINI_SCREENSHOT_ISOLATED") != "1")
        if os.environ.get("CASSINI_SCREENSHOT_ISOLATED") != "1":
            if self.in_flatpak:
                self._refresh_companion()
            else:
                self._start_browser_watch()
        from .sherpa_runtime import available as parakeet_available

        if self.writing_available and parakeet_available():
            from .speech_models import SpeechModels

            self.speech_models = SpeechModels(
                self.recording_settings.get_string("speech-model"),
                lambda value: self.recording_settings.set_string("speech-model", value),
                lambda: GLib.idle_add(self._models_changed),
            )
            from .diarization_models import DiarizationModels
            from .transcription import bundle_path

            self.diarization_models = DiarizationModels(bundle_path(), lambda: GLib.idle_add(self._models_changed))
            from .voice_models import VoiceModels

            self.voice_models = VoiceModels(
                bundle_path(),
                lambda: GLib.idle_add(self._models_changed),
                enabled=lambda: bool(self.diarization_models.snapshot().get("ready")),
            )
        import threading

        threading.Thread(target=self._load_voices, name="cassini-voices-startup", daemon=True).start()
        from .library_relocator import LibraryRelocator

        self.library_relocator = LibraryRelocator(self)
        if not os.environ.get("CASSINI_SCREENSHOT_ISOLATED") and not os.environ.get("CASSINI_ISOLATED_AUDIO"):
            from .global_shortcut import GlobalShortcut

            self.global_shortcut = GlobalShortcut(self.on_global_record, settings=self.recording_settings)

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
        from .capture_guard import refresh_capture_background

        refresh_capture_background(self)

    def do_open(self, files, _n_files, _hint):
        for file in files:
            self.open_in_new_window(file)

    def open_in_new_window(self, file):
        """Import/open *file* in the Library, presenting its window."""
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

    @property
    def in_flatpak(self):
        from .companion import in_flatpak

        return in_flatpak()

    @property
    def browser_companion_available(self):
        return self._companion_ready

    def _start_browser_watch(self):
        if self.browser_watch is None and os.environ.get("CASSINI_SCREENSHOT_ISOLATED") != "1":
            from .browser_watch import BrowserWatch

            self.browser_watch = BrowserWatch(self._browser_changed)

    def _refresh_companion(self):
        self._companion_probe_timer = 0
        if self._quitting or self._companion_ready or self._companion_probe_future is not None:
            return GLib.SOURCE_REMOVE
        from .companion import probe

        future = self._companion_probe_executor.submit(probe)
        self._companion_probe_future = future
        future.add_done_callback(lambda f: GLib.idle_add(self._companion_probed, f))
        return GLib.SOURCE_REMOVE

    def _companion_probed(self, future):
        self._companion_probe_future = None
        if self._quitting:
            return GLib.SOURCE_REMOVE
        retryable = True
        try:
            self._companion_ready, retryable = future.result()
        except Exception:
            logging.getLogger(__name__).exception("Could not probe the host browser bridge")
        if self._companion_ready:
            self._start_browser_watch()
            for window in self.get_windows():
                if hasattr(window, "companion_available_changed"):
                    window.companion_available_changed()
            if self.settings_dialog and not self.settings_dialog.closed:
                self.settings_dialog.refresh()
        elif retryable:
            self._companion_probe_timer = GLib.timeout_add_seconds(5, self._refresh_companion)
        return GLib.SOURCE_REMOVE

    def on_companion_setup(self, *_args, parent=None):
        if not self.browser_companion_available:
            return None
        from .companion_setup import CompanionSetup

        dialog = getattr(self, "companion_setup", None)
        if dialog is None or dialog.closed:
            dialog = self.companion_setup = CompanionSetup(self)
        dialog.present(parent or self.get_active_window())
        return dialog

    @property
    def recording_settings(self):
        return Gio.Settings.new(self.settings_schema)

    @property
    def recorder(self):
        """The earliest active native capture for existing meter clients."""
        from .capture_guard import recorder_busy

        active = [r for r in self.native_captures.values() if recorder_busy(r)]
        return min(active, key=lambda r: getattr(r, "_start_us", 0)) if active else self._last_native

    @recorder.setter
    def recorder(self, value):
        # Older embedded clients can supply a controller, without displacing
        # another session. All recording control enumerates the registry.
        if value is not None:
            self.native_captures[str(uuid4())] = value
        self._last_native = value

    def native_recorder_for(self, directory):
        from pathlib import Path

        return next(
            (
                r
                for r in self.native_captures.values()
                if r.session and r.session.directory.resolve() == Path(directory).resolve()
            ),
            None,
        )

    def set_auto_recording_hold(self, enabled):
        enabled = bool(enabled) and not self._quitting
        if enabled and not self._auto_recording_held:
            self.hold()
            self._auto_recording_held = True
        elif not enabled and self._auto_recording_held:
            self._auto_recording_held = False
            self.release()

    def begin_auto_recording_change(self, enabled):
        self._auto_recording_request += 1
        token = self._auto_recording_request
        self._auto_recording_pending = (token, enabled)
        self.hold()  # Keep an accepted preference write alive after its dialog closes.
        self.set_auto_recording_hold(enabled)
        return token

    def finish_auto_recording_change(self, token, snapshot):
        if self._auto_recording_pending and self._auto_recording_pending[0] == token:
            self._auto_recording_pending = None
            if snapshot is not None:
                self._browser_changed(snapshot)
            else:
                self.set_auto_recording_hold(self._browser_snapshot.get("autoRecord", False))
        self.release()
        return False

    def _browser_changed(self, snapshot):
        snapshot = dict(snapshot)
        revision = snapshot.get("autoRecordRevision", 0)
        if type(revision) is not int or revision < self._auto_recording_revision:
            snapshot["autoRecord"] = self._browser_snapshot.get("autoRecord", False)
        else:
            self._auto_recording_revision = revision
        self._browser_snapshot = snapshot
        self.observe_browser_recordings(snapshot, self._browser_seen_saved)
        self._browser_seen_saved = {r["path"] for r in snapshot.get("recordings", []) if isinstance(r.get("path"), str)}
        desired = self._auto_recording_pending
        self.set_auto_recording_hold(desired[1] if desired else snapshot.get("autoRecord", False))
        for window in self.get_windows():
            window._browser_snapshot = snapshot
        self._native_changed()

    def browser_capture_active(self):
        return any(s.get("recording") for s in self._browser_snapshot.get("statuses", [])) or any(
            (getattr(window, "_activity_source", None) and window._activity_source.snapshot)
            or any(status.get("recording") for status in getattr(window, "_browser_snapshot", {}).get("statuses", []))
            for window in self.get_windows()
        )

    def on_record(self, *_args):
        from .capture_guard import recorder_busy

        if (
            self._record_start_future
            or self.browser_capture_active()
            or any(recorder_busy(r) for r in self.native_captures.values())
        ):
            self.on_stop_recording()
            return
        self.do_activate()
        if self.recording_settings.get_boolean("skip-record-first"):
            self.start_recordings()
        elif not self.record_first:
            from .record_first import RecordFirst

            self.record_first = RecordFirst(self, self.get_active_window())
            self.record_first.connect("closed", lambda *_: setattr(self, "record_first", None))

    def on_global_record(self, *_):
        from .capture_guard import recorder_busy

        active = (
            self._record_start_future
            or any(recorder_busy(r) for r in self.native_captures.values())
            or self.browser_capture_active()
        )
        if (
            not active
            and self.recording_settings.get_boolean("skip-record-first")
            and self.get_windows()
            and not any(w.get_visible() for w in self.get_windows())
        ):
            self.start_recordings()
        else:
            self.on_record()

    def start_recordings(self):
        from . import browser_service

        if self._record_start_future is not None or self._quitting:
            return
        if not self.browser_companion_available:
            self.start_native()
            return
        self._record_start_generation += 1
        generation = self._record_start_generation
        group = self._record_start_group = str(uuid4())
        for window in self.get_windows():
            player = getattr(window, "player", None)
            if player is not None and player.playing:
                player.pause()
        self.hold()

        def start():
            target = browser_service.ensure_service()
            return browser_service.request("start-all", path=target, groupId=group)

        self._record_start_future = self._record_start_executor.submit(start)
        self._record_start_future.add_done_callback(
            lambda future: GLib.idle_add(self._recordings_started, future, generation)
        )

    def _recordings_started(self, future, generation):
        self._record_start_future = None
        self._record_start_group = None
        try:
            snapshot = future.result()
            if (
                generation != self._record_start_generation
                or self._quitting
                or self._capture_exiting
                or snapshot.get("cancelled")
            ):
                return False
            self._browser_changed(snapshot)
            active = [s for s in snapshot.get("statuses", []) if s.get("recording") and isinstance(s.get("path"), str)]
            calls = [m for m in snapshot.get("meetings", []) if m.get("sourceCount", 0) > 0]
            if not calls and not active:
                self.start_native()
            elif snapshot.get("failures"):
                self.recording_error = snapshot["failures"][0]["error"]
            elif active and any(w.get_visible() for w in self.get_windows()):
                self._pending_recording = active[0]["path"]
            self._native_changed()
        except CancelledError:
            pass
        except (OSError, ValueError, TimeoutError) as error:
            if generation == self._record_start_generation and not self._quitting:
                self.recording_error = str(error)
                self._native_changed()
        finally:
            self.release()
        return False

    def start_native(self, *, test=False):
        from .native_recorder import NativeRecorder

        recorder = NativeRecorder()
        self.native_captures[str(uuid4())] = recorder
        self._last_native = recorder
        recorder.connect("started", self._native_started)
        recorder.connect("finished", self._native_finished)
        recorder.connect("error", self._native_error)
        recorder.connect("changed", self._native_changed)
        self.recording_error = None
        for window in self.get_windows():
            player = getattr(window, "player", None)
            if player is not None and player.playing:
                player.pause()
        if not self._native_held:
            self.hold()
            self._native_held = True
        recorder.start(test=test)

    def change_recording_sources(self, meeting, me, others):
        from . import browser_service

        key = str(meeting.directory)
        if self._quitting or self._capture_exiting or key in self._source_changes:
            raise ValueError("A source change is not available")
        native = self.native_recorder_for(meeting.directory)
        previous_me = self.comfort.sources(meeting).me
        self.hold()
        try:
            future = (
                native.select(me, others)
                if native
                else self._record_start_executor.submit(
                    browser_service.request, "selection", directory=key, me=me, others=others
                )
            )
        except Exception:
            self.release()
            raise
        self._source_changes[key] = future
        future.add_done_callback(lambda f: GLib.idle_add(self._recording_sources_changed, key, f, previous_me))
        self._native_changed()
        return future

    def _recording_sources_changed(self, key, future, previous_me):
        self._source_changes.pop(key, None)
        try:
            from .session_events import source_state

            result = future.result()
            current = source_state(result["events"]) if isinstance(result, dict) else result
            if current.me != previous_me:
                # A rapid browser off/on can finish before the next inventory
                # poll observes the off state. Start a new 20-second silence
                # interval from the acknowledged microphone choice anyway.
                self.comfort.clocks.pop(Path(key).name, None)
        except Exception as error:
            logging.getLogger(__name__).warning("Could not change recording sources: %s", error)
        finally:
            self.release()
        for window in self.get_windows():
            if getattr(window, "monitor", None) is not None and not window.monitor._closed:
                window.monitor.refresh({Path(key)})
        self._native_changed()
        return False

    def _native_started(self, _recorder, path):
        self._pending_recording = path
        if not self._recording_cookie:
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
        from .capture_guard import recorder_busy

        if any(recorder_busy(r) for r in self.native_captures.values()):
            return
        if self._recording_cookie:
            self.uninhibit(self._recording_cookie)
            self._recording_cookie = 0
        if self._native_held:
            self._native_held = False
            self.release()

    def _native_finished(self, _recorder, _path):
        self.native_captures = {key: r for key, r in self.native_captures.items() if r is not _recorder}
        self._last_native = _recorder
        _recorder.close()
        self._pending_recording = None
        if (
            self.transcription_enabled
            and not self._quitting
            and not self._capture_exiting
            and _recorder.session is not None
            and any(track["frames"] > 0 for track in _recorder.session.manifest["tracks"])
        ):
            self.write_down(_path)
        self._release_native()
        self._native_changed()

    def _native_error(self, _recorder, message):
        self.recording_error = message
        if not _recorder.pipeline and not _recorder.starting:
            self._release_native()
        self._native_changed()

    def stop_native(self, *_, directory=None):
        from pathlib import Path

        for recorder in tuple(self.native_captures.values()):
            if (
                directory is None
                or recorder.session
                and recorder.session.directory.resolve() == Path(directory).resolve()
            ):
                recorder.stop()

    def stop_browser_recordings(self, directory=None):
        from concurrent.futures import ThreadPoolExecutor
        from pathlib import Path

        from .capture_guard import stop_browser_captures

        key = str(Path(directory).resolve()) if directory is not None else None
        if None in self._browser_stops or key in self._browser_stops:
            return
        self._browser_stops.add(key)
        self.hold()
        executor = ThreadPoolExecutor(max_workers=1)
        future = executor.submit(
            stop_browser_captures,
            directory,
            cancel_groups=[self._record_start_group] if self._record_start_group and directory is None else [],
        )

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
                finally:
                    self._browser_stops.discard(key)
                    self.release()
                self._native_changed()
                return False

            GLib.idle_add(delivered)

        future.add_done_callback(done)

    @staticmethod
    def _writing_unavailable():
        from .transcription import unavailable_reason

        reason = unavailable_reason()
        if reason:
            # The action is hidden; this is the only place the reason shows.
            logging.getLogger(__name__).warning("Writing down is not available on this computer: %s", reason)
        return reason

    @property
    def writing_available(self):
        """Whether to offer "Write it down" and "Try again" at all."""
        return self.writing_unavailable is None

    def _ensure_transcriber(self):
        from .transcription import TranscriptionQueue

        if self.transcriber is None:
            generation = self._transcription_generation

            def changed(_directory):
                GLib.idle_add(self._transcription_changed)

            def finished(_directory):
                GLib.idle_add(self._transcription_finished, generation)

            self.transcriber = TranscriptionQueue(
                changed,
                finished,
                models=self.speech_models,
                diarization_models=self.diarization_models,
                voice_models=self.voice_models,
                voice_store=self.voice_store,
            )
        return self.transcriber

    def recompute_voices(self, directory, affected):
        if self._quitting or self._capture_exiting:
            return

        def failed(_error):
            def show():
                from .strings import text

                for window in self.get_windows():
                    if hasattr(window, "toasts"):
                        toast = Adw.Toast(
                            title=GLib.markup_escape_text(text("savedVoices.changeFailed")),
                            button_label=text("common.tryAgain"),
                        )
                        toast.connect("button-clicked", lambda *_: self.recompute_voices(directory, affected))
                        window.toasts.add_toast(toast)
                return False

            GLib.idle_add(show)

        queue = self._ensure_transcriber()
        if queue.recompute_voices(directory, affected, failed):
            self._transcription_holds += 1
            self.hold()

    def write_down(self, meeting, *, resume=False):
        if self._quitting or self._capture_exiting:
            return
        self._ensure_transcriber()
        directory = getattr(meeting, "directory", meeting)
        if self.transcriber.enqueue(directory, resume=resume):
            self._transcription_holds += 1
            self.hold()

    def _load_voices(self):
        try:
            self.voice_store.refresh()
        except (OSError, ValueError):
            logging.exception("Could not load private saved voices")

    def _voices_changed(self):
        if self.settings_dialog and not self.settings_dialog.closed:
            self.settings_dialog.render_voices()
        matches = self.voice_store.match_signature()
        if matches != self._voice_matches:
            self._voice_matches = matches
            self._transcription_changed()
        return False

    def _models_changed(self):
        if self.settings_dialog and not self.settings_dialog.closed:
            self.settings_dialog.render_models()
        return False

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
        if snapshot.unavailable:
            return  # An unreadable Library is not evidence that meetings left it.
        keys = frozenset(m.directory.name for m in snapshot.meetings)
        if keys != self._voice_library_keys:
            for window in self.get_windows():
                if hasattr(window, "monitor") and not window.monitor._closed:
                    window.monitor._executor.submit(self.voice_store.sync_library, keys)
                    self._voice_library_keys = keys
                    break
        if self.settings_dialog and not self.settings_dialog.closed and self.settings_dialog.saved_voices_page:
            self.settings_dialog.saved_voices_page.refresh()
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
        self._record_start_generation += 1
        self.stop_native()
        if self.browser_capture_active() or self._record_start_future:
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
        if self._companion_probe_timer:
            GLib.source_remove(self._companion_probe_timer)
            self._companion_probe_timer = 0
        self._record_start_generation += 1
        if self.browser_watch:
            self.browser_watch.close()
        self.set_auto_recording_hold(False)
        self._record_start_executor.shutdown(wait=False, cancel_futures=True)
        self._companion_probe_executor.shutdown(wait=False, cancel_futures=True)
        if self.speech_models:
            self.speech_models.close()
        if self.diarization_models:
            self.diarization_models.close()
        if self.voice_models:
            self.voice_models.close()
        if self.library_relocator:
            self.library_relocator.close()
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
        if self._closing_background_windows:
            return
        if (
            not self._auto_recording_held
            and not self._capture_background_active()
            and not any(w is not window for w in self.get_windows())
        ):
            self.begin_exit()

    def _capture_background_active(self):
        from .capture_guard import capture_targets

        return getattr(self, "_capture_background", False) and not self._quitting and any(capture_targets(self))

    def do_shutdown(self):
        if self.global_shortcut:
            self.global_shortcut.close()
        if self.comfort:
            self.comfort.close()
        self.begin_exit()
        Adw.Application.do_shutdown(self)

    def move_library(self, destination, parent):
        from .capture_guard import capture_targets
        from .strings import text

        if any(capture_targets(self)):
            dialog = Adw.AlertDialog(heading=text("confirm.moveLibrary.title"), body=text("confirm.moveLibrary.body"))
            dialog.add_response("cancel", text("common.cancel"))
            dialog.add_response("move", text("confirm.moveLibrary.primary"))
            dialog.set_response_appearance("move", Adw.ResponseAppearance.SUGGESTED)
            dialog.set_default_response("move")
            dialog.set_close_response("cancel")

            def response(_dialog, choice):
                self._move_confirmation = None
                if choice == "move":
                    self.library_relocator.move(destination)

            dialog.connect("response", response)
            self._move_confirmation = dialog
            dialog.present(self.get_active_window())
        else:
            self.library_relocator.move(destination)

    def on_preferences(self, *_args):
        from .settings_dialog import SettingsDialog

        if self.settings_dialog is None or self.settings_dialog.closed:
            self.settings_dialog = SettingsDialog(self)
        self.settings_dialog.present(self.get_active_window())

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
        from .model_download import has_downloaded_models, model_root
        from .strings import text

        notice = model_root() / "NOTICE.txt"
        if notice.is_file() and has_downloaded_models():
            uri = notice.as_uri()
            about.add_link(text("about.modelNotices"), uri)

            def local_notice(_dialog, target):
                if target == uri:
                    Gtk.FileLauncher(file=Gio.File.new_for_path(str(notice))).launch(
                        self.get_active_window(), None, None
                    )
                    return True
                return False

            about.connect("activate-link", local_notice)
        about.present(self.get_active_window())
        return about


def main(application_id, version, profile):
    """The application's entry point."""
    # Otherwise --help says "Usage: python3".
    GLib.set_prgname("cassini-viewer")
    app = CassiniApplication(application_id, version, profile)
    return app.run(sys.argv)
