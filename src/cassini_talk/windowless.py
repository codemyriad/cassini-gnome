# SPDX-License-Identifier: GPL-3.0-or-later
"""D41 Background Apps presence while automatic recording is armed."""

from gi.repository import Gio

from .strings import text


class WindowlessBackground:
    def __init__(self, app):
        self.app = app
        self.message = ""
        self.idle = False
        self.attempted = False
        self.refused = False

    def refresh(self, recording_message="", timer=""):
        app = self.app
        windowless = not any(window.get_visible() for window in app.get_windows())
        from .capture_guard import capture_targets

        recording = bool(recording_message) or any(capture_targets(app))
        indicator = getattr(app, "status_notifier", None)
        if recording and not recording_message:
            from .ux_formats import live_big

            recording_message = (
                indicator.message if indicator and indicator.recording else text("hidden.status", liveBig=live_big(0))
            )
            timer = indicator.label if indicator and indicator.recording else live_big(0)
        if indicator:
            indicator.update(
                recording=recording,
                message=recording_message,
                label=timer if recording else "",
                windowless=windowless,
                automatic=app._auto_recording_held and not app._quitting,
            )
        self.idle = windowless and app._auto_recording_held and not recording_message and not app._quitting
        self.message = (
            (recording_message or (text("hidden.status.watching") if self.idle else ""))
            if windowless and not app._quitting
            else ""
        )
        if not self.message:
            self.attempted = self.refused = False
            app.background_recording.status("")
            return
        if app.background_recording.granted:
            self._publish()
        elif self.refused and self._indicator_route():
            self._published(False)
        elif app.background_recording.callback is None and not self.attempted:
            self.attempted = True
            app.background_recording.request(self._permitted)

    def _indicator_route(self):
        """A native app whose top-bar item a host accepted stays reachable without the portal."""
        indicator = getattr(self.app, "status_notifier", None)
        return not self.app.in_flatpak and bool(getattr(indicator, "available", False))

    def _permitted(self, allowed):
        if allowed:
            self._publish()
            return
        self.refused = True
        indicator = getattr(self.app, "status_notifier", None)
        if self._indicator_route():
            self._published(False)
        elif not self.app.in_flatpak and getattr(indicator, "registering", False):
            return  # indicator_ready() chooses once the host answers.
        else:
            self._reopen()

    def _reopen(self):
        if self.message and not self.app._quitting:
            # A denied or failed request must leave a reachable window, rather
            # than an invisible automatic recorder the desktop may terminate.
            # Flatpak always needs the portal; a native app needs it only
            # without an accepted top-bar item.
            self.app.activate()

    def _publish(self):
        self.app.background_recording.status(self.message, self._published)

    def _published(self, available):
        app = self.app
        if not self.idle or not self.message or app._quitting:
            return
        if not app.background_recording.granted and not (self.refused and self._indicator_route()):
            return
        flatpak = app.in_flatpak
        background_available = available and flatpak
        indicator = getattr(app, "status_notifier", None)
        # A duplicate status call can return False while SetStatus is still
        # pending. Wait for a confirmed result before choosing a route.
        waiting_for_portal = flatpak and not available and app.background_recording.status_supported
        if not background_available and (waiting_for_portal or getattr(indicator, "registering", False)):
            return
        body = (
            "hidden.firstClose.body"
            if background_available
            else "hidden.firstClose.indicatorBody"
            if getattr(indicator, "available", False)
            else "hidden.firstClose.launcherBody"
        )
        settings = app.recording_settings
        if settings.get_boolean("background-notification-shown"):
            return
        notification = Gio.Notification.new(text("hidden.firstClose.title"))
        notification.set_body(text(body))
        notification.set_default_action("app.open-recording")
        app.send_notification("background", notification)
        settings.set_boolean("background-notification-shown", True)

    def indicator_ready(self):
        # SetStatus is unavailable for native builds. A late SNI registration
        # can finish a deferred notification, but never one after reopening.
        if not self.app.in_flatpak or not self.app.background_recording.status_supported:
            self._published(False)
        indicator = getattr(self.app, "status_notifier", None)
        if self.refused and not self._indicator_route() and not getattr(indicator, "registering", False):
            self._reopen()
