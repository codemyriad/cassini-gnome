# SPDX-License-Identifier: GPL-3.0-or-later
"""Live observations and hidden recording notifications, owned by the application."""

import logging
import time
from concurrent.futures import ThreadPoolExecutor

from gi.repository import Gio, GLib

from . import ux_formats as fmt
from .recording_details import SilenceClock, recording_observation
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
        self.clocks = {}
        self.futures = {}
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cassini-live-details")
        self.timer = GLib.timeout_add_seconds(1, self.tick) if automatic else 0
        self.closed = False
        self.warning_shown = set()
        self.notification_visible = False
        self.last_payload = None

    def active(self):
        return {
            m.key: m
            for w in self.app.get_windows()
            if getattr(w, "snapshot", None)
            for m in w.snapshot.meetings
            if m.lifecycle in ("recording", "saving")
        }

    def status(self, meeting):
        return self.states.get(meeting.key, dict(levels=[0.0, 0.0], voices=0, silent=False, line=None, minutes=0))

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
        native = self.app.recorder
        for key, meeting in meetings.items():
            state = self.states.setdefault(key, dict(levels=[0.0, 0.0], voices=0, silent=False, line=None, minutes=0))
            if meeting.capture_source == "gnome" and native and native.pipeline is not None:
                state.update(levels=list(native.levels), minutes=native.minutes_left, line=native.line)
            elif key not in self.futures and meeting.lifecycle == "recording":
                future = self.executor.submit(recording_observation, meeting)
                self.futures[key] = future
                future.add_done_callback(lambda f, m=meeting: GLib.idle_add(self._observed, m, f))
            clock = self.clocks.setdefault(key, SilenceClock(now))
            state["silent"] = clock.update(state["levels"][0], now)
            if state["line"]:
                self._disk_notification(meeting, state["line"], state["minutes"])
        # Native stopped-at-reserve remains visible after finalization.
        if native and native.session and native.line == "errors.diskStopped.line":
            key = native.session.directory.name
            self.states.setdefault(key, {}).update(line=native.line, minutes=native.minutes_left)
            self._disk_notification(None, native.line, native.minutes_left, key=key)
        self._refresh_pages()
        hidden = bool(meetings) and not any(w.get_visible() for w in self.app.get_windows())
        if hidden:
            meeting = next(iter(meetings.values()))
            payload = notification_payload(meeting, self.elapsed(meeting), native=meeting.capture_source == "gnome")
            self.last_payload = payload
            notification = Gio.Notification.new(payload["title"])
            notification.set_body(payload["body"])
            notification.set_icon(Gio.ThemedIcon.new("media-record-symbolic"))
            notification.set_default_action(payload["defaultAction"])
            for button in payload["buttons"]:
                notification.add_button(button["label"], button["action"])
            self.app.send_notification("recording", notification)
            self.notification_visible = True
        elif self.notification_visible:
            self.app.withdraw_notification("recording")
            self.notification_visible = False
        if (
            not meetings
            and not (native and (native.pipeline or native.starting))
            and getattr(self.app, "_capture_background", False)
        ):
            self.app._capture_background = False
            self.app.release()
        return True

    def _observed(self, meeting, future):
        self.futures.pop(meeting.key, None)
        if self.closed or meeting.key not in self.active():
            return False
        try:
            value = future.result()
        except (OSError, ValueError) as error:
            LOG.warning("Could not observe live recording: %s", error)
            return False
        state = self.states[meeting.key]
        state.update(levels=value["levels"], voices=value["voices"], minutes=value["minutes"])
        if value["reserve"]:
            state["line"] = "errors.diskStopped.line"
            self.app.stop_browser_recordings()
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
        if self.timer:
            GLib.source_remove(self.timer)
            self.timer = 0
        if self.notification_visible:
            self.app.withdraw_notification("recording")
        self.executor.shutdown(wait=False, cancel_futures=True)
