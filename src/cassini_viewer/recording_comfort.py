# SPDX-License-Identifier: GPL-3.0-or-later
"""Live observations and hidden recording notifications, owned by the application."""

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from gettext import gettext as _

from gi.repository import Gio, GLib, Gtk

from . import ux_formats as fmt
from .capture_guard import recorder_busy
from .recording_details import SilenceClock, edit_recording, live_meeting, live_observation
from .strings import CATALOG, text

LOG = logging.getLogger(__name__)


def notification_payload(meeting, elapsed, *, native=False):
    # Native copy is published by the coordinator; never claim a browser path.
    identifier = "hidden.notification.nativeBody" if native else "hidden.notification.body"
    return dict(
        title=text("hidden.status", liveBig=fmt.live_big(elapsed)),
        body=text(identifier, title=meeting.title) if identifier in CATALOG else meeting.title,
        buttons=[
            dict(label=text("common.stop"), action="app.stop-recording"),
            dict(label=text("hidden.openCassini"), action="app.open-recording"),
        ],
        defaultAction="app.open-recording",
    )


class RecordingComfort:
    def __init__(self, app, *, automatic=True):
        self.app = app
        self.states = {}
        self.known = {}
        self.clocks = {}
        self.futures = {}
        self.loading = {}
        self.editor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cassini-meeting-edits")
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cassini-live-details")
        self.timer = GLib.timeout_add_seconds(1, self.tick) if automatic else 0
        self.closed = False
        self.warning_shown = set()
        self.notification_visible = False
        self.last_payload = None
        self.browser_cookie = 0
        self._browser_inhibit_requested = False

    def edit(self, directory, **changes):
        return self.editor.submit(edit_recording, directory, **changes)

    def active(self):
        from pathlib import Path

        from .library import library_root, meeting_id

        controllers = {}
        for window in self.app.get_windows():
            controllers.update(getattr(window, "_activity", {}))
            for status in getattr(window, "_browser_snapshot", {}).get("statuses", []):
                if not status.get("recording") or not isinstance(status.get("path"), str):
                    continue
                directory = Path(status["path"]).resolve()
                identifier = meeting_id(directory)
                if directory.parent != library_root().resolve() or identifier is None:
                    continue
                controllers[identifier] = "recording"
                if directory.name not in self.known and directory.name not in self.loading:
                    future = self.executor.submit(live_meeting, directory)
                    self.loading[directory.name] = future
                    future.add_done_callback(lambda f, key=directory.name: GLib.idle_add(self._loaded, key, f))
            if getattr(window, "snapshot", None):
                self.known.update(
                    {m.key: m for m in window.snapshot.meetings if m.lifecycle in ("recording", "saving")}
                )
        result = {}
        native = self.app.recorder
        for key, meeting in self.known.items():
            if meeting.id not in controllers:
                continue
            if meeting.capture_source == "gnome" and not (
                recorder_busy(native) and native.session and native.session.directory == meeting.directory
            ):
                continue
            result[key] = meeting
        self.known = result
        return result

    def status(self, meeting):
        state = dict(
            self.states.get(meeting.key, dict(levels=[0.0, 0.0], voices=0, silent=False, line=None, minutes=0))
        )
        native = self.app.recorder
        if native and native.session and native.session.directory == meeting.directory:
            state.update(levels=list(native.levels), line=native.line, minutes=native.minutes_left)
        return state

    def native_level(self):
        native = self.app.recorder
        if native and native.session:
            key = native.session.directory.name
            state = self.states.setdefault(key, dict(levels=[0.0, 0.0], voices=0, silent=False, line=None, minutes=0))
            state.update(levels=list(native.levels), line=native.line, minutes=native.minutes_left)
            if native.line:
                self._disk_notification(None, native.line, native.minutes_left, key=key)
            if native.pipeline is not None:
                now = GLib.get_monotonic_time() / 1e6
                self.clocks.setdefault(key, SilenceClock(now)).update(native.levels[0], now)

    def elapsed(self, meeting):
        native = self.app.recorder
        if native and native.pipeline is not None and native.session.directory == meeting.directory:
            return max(0, (GLib.get_monotonic_time() - native._start_us) // 1000)
        window = next((w for w in self.app.get_windows() if hasattr(w, "now_ms")), None)
        now = window.now_ms if window else int(time.time() * 1000)
        return max(0, now - (meeting.started_at_ms or now))

    def tick(self):
        if self.closed:
            return False
        meetings = self.active()
        now = GLib.get_monotonic_time() / 1e6
        browser_active = self.app.browser_capture_active()
        if browser_active and not self._browser_inhibit_requested:
            self._browser_inhibit_requested = True
            self.browser_cookie = self.app.inhibit(
                self.app.get_active_window(), Gtk.ApplicationInhibitFlags.SUSPEND, _("Recording a meeting")
            )
        elif not browser_active and self._browser_inhibit_requested:
            if self.browser_cookie:
                self.app.uninhibit(self.browser_cookie)
                self.browser_cookie = 0
            self._browser_inhibit_requested = False
        native = self.app.recorder
        for key, meeting in meetings.items():
            state = self.states.setdefault(key, dict(levels=[0.0, 0.0], voices=0, silent=False, line=None, minutes=0))
            if meeting.capture_source == "gnome" and native and native.pipeline is not None:
                state.update(levels=list(native.levels), minutes=native.minutes_left, line=native.line)
            elif key not in self.futures and meeting.lifecycle == "recording":
                future = self.executor.submit(live_observation, meeting)
                self.futures[key] = future
                future.add_done_callback(lambda f, m=meeting: GLib.idle_add(self._observed, m, f))
            clock = self.clocks.setdefault(key, SilenceClock(now))
            state["silent"] = clock.update(state["levels"][0], now)
            if state["line"]:
                self._disk_notification(meeting, state["line"], state["minutes"])
        # Native stopped-at-reserve remains visible after finalization.
        if native and native.session and native.line == "errors.diskStopped.line":
            key = native.session.directory.name
            state = self.states.setdefault(key, dict(levels=[0.0, 0.0], voices=0, silent=False, line=None, minutes=0))
            state.update(line=native.line, minutes=native.minutes_left)
            self._disk_notification(None, native.line, native.minutes_left, key=key)
        self._refresh_pages()
        hidden = bool(meetings) and not any(w.get_visible() for w in self.app.get_windows())
        if hidden:
            meeting = next(
                (m for m in meetings.values() if native and native.session and m.directory == native.session.directory),
                next(iter(meetings.values())),
            )
            payload = notification_payload(meeting, self.elapsed(meeting), native=meeting.capture_source == "gnome")
            self.last_payload = payload
            notification = Gio.Notification.new(payload["title"])
            notification.set_body(payload["body"])
            if self.notification_visible:
                notification.set_priority(Gio.NotificationPriority.LOW)
            notification.set_icon(Gio.ThemedIcon.new("media-record-symbolic"))
            notification.set_default_action(payload["defaultAction"])
            for button in payload["buttons"]:
                notification.add_button(button["label"], button["action"])
            self.app.send_notification("recording", notification)
            self.notification_visible = True
        elif self.notification_visible:
            self.app.withdraw_notification("recording")
            self.notification_visible = False
        return True

    def _loaded(self, key, future):
        self.loading.pop(key, None)
        if self.closed:
            return False
        try:
            meeting = future.result()
        except (OSError, ValueError) as error:
            LOG.warning("Could not read live browser capture: %s", error)
            return False
        if meeting:
            self.known[key] = meeting
            self.tick()
        return False

    def _observed(self, meeting, future):
        self.futures.pop(meeting.key, None)
        if self.closed or meeting.key not in self.active():
            return False
        try:
            current, value = future.result()
            self.known[meeting.key] = current
        except (OSError, ValueError) as error:
            LOG.warning("Could not observe live recording: %s", error)
            return False
        state = self.states[meeting.key]
        state.update(levels=value["levels"], voices=value["voices"], minutes=value["minutes"])
        if value["recent_sound"]:
            now = GLib.get_monotonic_time() / 1e6
            self.clocks.setdefault(meeting.key, SilenceClock(now)).update(1, now)
        if value["reserve"]:
            state["line"] = "errors.diskStopped.line"
            self.app.stop_browser_recordings(meeting.directory)
        elif value["minutes"] < 30:
            state["line"] = "errors.diskLow.line"
        else:
            state["line"] = None
        if state["line"]:
            self._disk_notification(meeting, state["line"], state["minutes"])
        self._refresh_pages()
        return False

    def _refresh_pages(self):
        for window in self.app.get_windows():
            if getattr(window, "recording_page", None):
                window.recording_page.update_status()
            if hasattr(window, "_recording_line"):
                window._recording_line(self.app.recorder)

    def _disk_notification(self, meeting, line, minutes, *, key=None):
        key = key or meeting.key
        identity = key, line
        if identity in self.warning_shown:
            return
        self.warning_shown.add(identity)
        notification = Gio.Notification.new(text(line, n=minutes))
        notification.set_default_action("app.open-recording")
        self.app.send_notification("recording-space", notification)

    def close(self):
        self.closed = True
        if self.browser_cookie:
            self.app.uninhibit(self.browser_cookie)
            self.browser_cookie = 0
        if self.timer:
            GLib.source_remove(self.timer)
            self.timer = 0
        if self.notification_visible:
            self.app.withdraw_notification("recording")
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.editor.shutdown(wait=False, cancel_futures=False)
