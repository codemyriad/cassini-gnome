# SPDX-License-Identifier: GPL-3.0-or-later
"""Live observations and hidden recording notifications, owned by the application."""

import logging
import time
from concurrent.futures import CancelledError, ThreadPoolExecutor
from dataclasses import replace

from gi.repository import Gio, GLib, Gtk

from . import ux_formats as fmt
from .capture_guard import (
    browser_capture_busy,
    capture_saving_only,
    capture_targets,
    native_recorders,
    recorder_busy,
    recorder_stopping,
)
from .i18n import gettext as _
from .recording_details import SilenceClock, edit_recording, live_meeting, live_observation
from .session_events import meeting_sources, read_events, source_state
from .strings import text

LOG = logging.getLogger(__name__)


def notification_payload(meeting, elapsed, *, native=False, saving=False, tz=None):
    saving = bool(saving or getattr(meeting, "lifecycle", None) == "saving")
    body = meeting.title
    if meeting.started_at_ms is not None:
        body += "\n" + text(
            "recording.since",
            time=fmt.row_time(
                meeting.started_at_ms, tz=tz or fmt.local_timezone(GLib.TimeZone.new_local().get_identifier())
            ),
        )
    buttons = (
        [dict(label=text("common.show"), action="app.open-recording")]
        if saving
        else [
            dict(label=text("common.stop"), action="app.stop-recording"),
            dict(label=text("common.show"), action="app.open-recording"),
        ]
    )
    payload = dict(
        title=text("details.end.saving" if saving else "recording.background.notification"),
        body=body,
        buttons=buttons,
        defaultAction="app.open-recording",
    )
    if meeting_sources(meeting).fallback:
        payload["body"] += "\n" + text("errors.othersFallback.line")
    return payload


class RecordingComfort:
    def __init__(self, app, *, automatic=True):
        self.app = app
        self.states = {}
        self.known = {}
        self.clocks = {}
        self.futures = {}
        self._observe_again = set()
        self.loading = {}
        self.drafts = {}
        self.editor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cassini-meeting-edits")
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cassini-live-details")
        self.timer = GLib.timeout_add_seconds(1, self.tick) if automatic else 0
        self.closed = False
        self.warning_shown = set()
        self.fallback_shown = set()
        self.notification_visible = False
        self.last_payload = None
        self.browser_cookie = 0
        self._browser_inhibit_requested = False

    def edit(self, directory, *, title):
        draft = dict(title=title.strip()[:500] or None)
        self.drafts.setdefault(str(directory), {}).update(draft)
        return self.editor.submit(edit_recording, directory, title=title)

    def draft(self, meeting):
        """Retain optimistic edits until the scanner has observed every field."""
        key = str(meeting.directory)
        draft = self.drafts.get(key, {})
        if draft and all(meeting.metadata.get(field) == value for field, value in draft.items()):
            self.drafts.pop(key, None)
            return {}
        return dict(draft)

    def _native_for(self, meeting):
        return next(
            (
                native
                for native in native_recorders(self.app)
                if native.session and native.session.directory == meeting.directory
            ),
            None,
        )

    def active(self):
        from pathlib import Path

        from .library import meeting_id
        from .library_location import owned_directory

        controllers, native_directories, broker_directories = {}, set(), set()

        def load(directory, lifecycle="recording"):
            identifier = meeting_id(directory)
            controllers[identifier] = lifecycle
            if directory.name not in self.known and directory.name not in self.loading:
                future = self.executor.submit(live_meeting, directory, lifecycle)
                self.loading[directory.name] = future
                future.add_done_callback(lambda f, key=directory.name: GLib.idle_add(self._loaded, key, f))

        for native in native_recorders(self.app):
            if recorder_busy(native) and native.session:
                directory = native.session.directory
                native_directories.add(directory)
                load(directory, "saving" if recorder_stopping(native) else "recording")
        snapshots = [getattr(self.app, "_browser_snapshot", {})]
        for window in self.app.get_windows():
            controllers.update(getattr(window, "_activity", {}))
            snapshots.append(getattr(window, "_browser_snapshot", {}))
            if getattr(window, "snapshot", None):
                for meeting in window.snapshot.meetings:
                    if meeting.lifecycle not in ("recording", "saving"):
                        continue
                    previous = self.known.get(meeting.key)
                    # Session events append as sources change. A cached Library
                    # scan can precede our completed live observation; replaying
                    # its shorter history must not undo an attested fallback.
                    if (
                        previous is not None
                        and len(previous.capture_events) > len(meeting.capture_events)
                        and previous.capture_events[: len(meeting.capture_events)] == meeting.capture_events
                    ):
                        continue
                    self.known[meeting.key] = meeting
        for snapshot in snapshots:
            for status in snapshot.get("statuses", []):
                if not browser_capture_busy(status) or not isinstance(status.get("path"), str):
                    continue
                try:
                    directory = owned_directory(Path(status["path"]))
                except ValueError:
                    continue
                broker_directories.add(directory)
                load(directory, "saving" if status.get("stopping") else "recording")
        self.known = {
            key: (
                meeting
                if meeting.lifecycle == controllers[meeting.id]
                else replace(meeting, lifecycle=controllers[meeting.id])
            )
            for key, meeting in self.known.items()
            if meeting.id in controllers
            and (meeting.capture_source != "gnome" or meeting.directory in native_directories | broker_directories)
        }
        return self.known

    def status(self, meeting):
        state = dict(
            self.states.get(meeting.key, dict(levels=[0.0, 0.0], voices=0, silent=False, line=None, minutes=0))
        )
        native = self._native_for(meeting)
        if native:
            state["output_muted"] = getattr(native, "output_muted", False)
            state.update(levels=list(native.levels), line=native.line, minutes=native.minutes_left)
        sources = self.sources(meeting)
        state["output_muted"] = bool(
            state.get("output_muted")
            and sources.others
            and sources.source == "system"
            and meeting.lifecycle == "recording"
        )
        if not sources.me:
            state["silent"] = False
        return state

    def sources(self, meeting):
        native = self._native_for(meeting)
        if native and native.session:
            return source_state(native.session.manifest.get("events"), default_source="system")
        return meeting_sources(self.known.get(meeting.key, meeting))

    def native_level(self):
        for native in native_recorders(self.app):
            if native.session:
                key = native.session.directory.name
                state = self.states.setdefault(
                    key, dict(levels=[0.0, 0.0], voices=0, silent=False, line=None, minutes=0)
                )
                state.update(levels=list(native.levels), line=native.line, minutes=native.minutes_left)
                if native.line:
                    self._disk_notification(None, native.line, native.minutes_left, key=key)
                if native.pipeline is not None:
                    now = GLib.get_monotonic_time() / 1e6
                    state["silent"] = self.clocks.setdefault(key, SilenceClock(now)).update(native.levels[0], now)

    def elapsed(self, meeting):
        native = self._native_for(meeting)
        if native and native.pipeline is not None:
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
            own_native = self._native_for(meeting)
            state = self.states.setdefault(key, dict(levels=[0.0, 0.0], voices=0, silent=False, line=None, minutes=0))
            if own_native and own_native.pipeline is not None:
                state.update(levels=list(own_native.levels), minutes=own_native.minutes_left, line=own_native.line)
            elif meeting.lifecycle == "recording":
                self._request_observation(meeting)
            clock = self.clocks.setdefault(key, SilenceClock(now))
            if self.sources(meeting).me:
                state["silent"] = clock.update(state["levels"][0], now)
            else:
                self.clocks.pop(key, None)
                state["silent"] = False
            if state["line"]:
                self._disk_notification(meeting, state["line"], state["minutes"])
            self._fallback_notification(meeting)
        # Native stopped-at-reserve remains visible after finalization.
        if native and native.session and native.line == "errors.diskStopped.line":
            key = native.session.directory.name
            state = self.states.setdefault(key, dict(levels=[0.0, 0.0], voices=0, silent=False, line=None, minutes=0))
            state.update(line=native.line, minutes=native.minutes_left)
            self._disk_notification(None, native.line, native.minutes_left, key=key)
        self._refresh_pages()
        self.refresh_background(meetings)
        return True

    def refresh_background(self, meetings=None):
        if meetings is None:
            meetings = self.active()
        saving = capture_saving_only(self.app)
        recording = bool(meetings) or saving or any(capture_targets(self.app))
        meeting = min(
            meetings.values(),
            key=lambda m: (
                m.lifecycle == "saving",
                m.started_at_ms if m.started_at_ms is not None else float("inf"),
                m.key,
            ),
            default=None,
        )
        if saving:
            timer = text("states.badge.saving")
            status = text("details.end.saving")
        elif recording:
            timer = fmt.live_big(self.elapsed(meeting) if meeting else 0)
            status = text("hidden.status", liveBig=timer)
        else:
            timer = status = ""
        self.app.windowless_background.refresh(status, timer)
        hidden = recording and not any(w.get_visible() for w in self.app.get_windows())
        if hidden:
            if meeting:
                payload = notification_payload(
                    meeting,
                    self.elapsed(meeting),
                    native=self.sources(meeting).source != "browser",
                    saving=saving,
                    tz=next((w.tz for w in self.app.get_windows() if hasattr(w, "tz")), None),
                )
                if (
                    any(self.sources(m).fallback for m in meetings.values())
                    and text("errors.othersFallback.line") not in payload["body"]
                ):
                    payload["body"] += "\n" + text("errors.othersFallback.line")
            else:
                # A new capture can precede Library discovery. Its controls
                # must already be available when the last window is hidden.
                title = text("details.end.saving" if saving else "recording.background.notification")
                buttons = (
                    [dict(label=text("common.show"), action="app.open-recording")]
                    if saving
                    else [
                        dict(label=text("common.stop"), action="app.stop-recording"),
                        dict(label=text("common.show"), action="app.open-recording"),
                    ]
                )
                payload = dict(
                    title=title,
                    body=title,
                    buttons=buttons,
                    defaultAction="app.open-recording",
                )
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
        if not hidden and self.notification_visible:
            self.app.withdraw_notification("recording")
            self.notification_visible = False

    def _loaded(self, key, future):
        self.loading.pop(key, None)
        if self.closed:
            return False
        try:
            meeting = future.result()
        except (OSError, ValueError) as error:
            LOG.warning("Could not read live browser capture: %s", error)
            return False
        if meeting and not self.app.meeting_trash.hides(meeting.directory):
            self.known[key] = meeting
            self.tick()
        return False

    def _request_observation(self, meeting):
        if meeting.key in self.futures:
            # A source-change tick must survive an older read's completion.
            # Several requests still coalesce to at most one follow-up read.
            self._observe_again.add(meeting.key)
            return
        future = self.executor.submit(live_observation, meeting)
        self.futures[meeting.key] = future
        future.add_done_callback(lambda f, m=meeting: GLib.idle_add(self._observed, m, f))

    def _observed(self, meeting, future):
        if self.futures.get(meeting.key) is not future:
            return False  # A discarded capture or superseded callback owns no update.
        self.futures.pop(meeting.key)
        again = meeting.key in self._observe_again
        self._observe_again.discard(meeting.key)
        if self.closed or meeting.key not in self.active():
            return False
        try:
            try:
                current, value = future.result()
                self.known[meeting.key] = current
            except (OSError, ValueError) as error:
                LOG.warning("Could not observe live recording: %s", error)
                return False
            state = self.states[meeting.key]
            state.update(
                levels=value["levels"],
                voices=value["voices"],
                minutes=value["minutes"],
                output_muted=value.get("output_muted", False),
                source_problem=value.get("source_problem", False),
            )
            if value["recent_sound"]:
                now = GLib.get_monotonic_time() / 1e6
                state["silent"] = self.clocks.setdefault(meeting.key, SilenceClock(now)).update(1, now)
            if value["reserve"]:
                state["line"] = "errors.diskStopped.line"
                self.app.stop_browser_recordings(meeting.directory, reason="disk")
            elif value["minutes"] < 30:
                state["line"] = "errors.diskLow.line"
            else:
                state["line"] = None
            if state["line"]:
                self._disk_notification(meeting, state["line"], state["minutes"])
            self._fallback_notification(current)
            self._refresh_pages()
            return False

        finally:
            if again and not self.closed:
                current = self.active().get(meeting.key)
                if current and current.lifecycle == "recording":
                    self._request_observation(current)

    def _fallback_notification(self, meeting):
        """Alert once for each committed automatic-capture loss, even with a window.

        Reading the session events rather than browser liveness also excludes
        pending/failed fallbacks and ordinary user Stop. An event's position
        distinguishes losses that share the same timestamp after a restore.
        """
        if meeting.lifecycle != "recording":
            return
        state = meeting_sources(meeting)
        if not (state.others and state.source == "system" and state.reason == "lost"):
            return
        if not any(
            status.get("automatic") is True
            and status.get("recording") is True
            and status.get("path") == str(meeting.directory)
            for status in (self.app._browser_snapshot or {}).get("statuses", [])
        ):
            return
        events = read_events(meeting.capture_events)
        index = max(i for i, event in enumerate(events) if event["type"] == "othersSource")
        key = (meeting.key, index)
        if key in self.fallback_shown:
            return
        notification = Gio.Notification.new(text("errors.othersFallback.line"))
        notification.set_body(meeting.title)
        notification.set_icon(Gio.ThemedIcon.new("audio-card-symbolic"))
        notification.set_default_action("app.open-recording")
        notification.add_button(text("common.show"), "app.open-recording")
        self.app.send_notification("recording-fallback-" + meeting.key, notification)
        self.fallback_shown.add(key)

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

    def saved_notification(self, directory):
        if self.closed or self.app._quitting:
            return
        self.app.hold()
        try:
            future = self.editor.submit(live_meeting, directory, "saved")
        except RuntimeError:
            self.app.release()
            return

        def delivered(result):
            try:
                meeting = result.result()
                if (
                    not self.closed
                    and not self.app._quitting
                    and meeting
                    and meeting.has_audio
                    and meeting.capture_state in ("completed", "interrupted")
                ):
                    notification = Gio.Notification.new(text("recording.savedNotification", title=meeting.title))
                    notification.set_default_action("app.open-recording")
                    self.app.send_notification("saved-" + meeting.key, notification)
            except (OSError, ValueError, CancelledError):
                LOG.warning("Could not read the saved recording for notification", exc_info=True)
            finally:
                self.app.release()
            return GLib.SOURCE_REMOVE

        future.add_done_callback(lambda result: GLib.idle_add(delivered, result))

    def discard_meeting(self, directory):
        key = directory.name
        self.drafts.pop(str(directory), None)
        self._observe_again.discard(key)
        for mapping in (self.known, self.states, self.clocks, self.futures, self.loading):
            value = mapping.pop(key, None)
            if hasattr(value, "cancel"):
                value.cancel()
        self.warning_shown.discard(key)
        self.fallback_shown = {entry for entry in self.fallback_shown if entry[0] != key}

    def close(self):
        self.closed = True
        self._observe_again.clear()
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
