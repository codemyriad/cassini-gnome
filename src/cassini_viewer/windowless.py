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

    def refresh(self, recording_message=""):
        app = self.app
        windowless = not any(window.get_visible() for window in app.get_windows())
        self.idle = windowless and app._auto_recording_held and not recording_message and not app._quitting
        self.message = (
            (recording_message or (text("hidden.status.watching") if self.idle else ""))
            if windowless and not app._quitting
            else ""
        )
        if not self.message:
            self.attempted = False
            app.background_recording.status("")
            return
        if app.background_recording.granted:
            self._publish()
        elif app.background_recording.callback is None and not self.attempted:
            self.attempted = True
            app.background_recording.request(self._permitted)

    def _permitted(self, allowed):
        if allowed:
            self._publish()
        elif self.message and not self.app._quitting:
            # A denied request must leave a reachable window, rather than an
            # invisible automatic recorder that the desktop may terminate.
            self.app.activate()

    def _publish(self):
        self.app.background_recording.status(self.message, self._published)

    def _published(self, available):
        app = self.app
        # In particular, never claim Background Apps can open a native build
        # when the host portal rejects SetStatus. D41/R48 records that limit.
        if not available or not self.idle or not self.message or app._quitting:
            return
        settings = app.recording_settings
        if settings.get_boolean("background-notification-shown"):
            return
        notification = Gio.Notification.new(text("hidden.firstClose.title"))
        notification.set_body(text("hidden.firstClose.body"))
        notification.set_default_action("app.open-recording")
        app.send_notification("background", notification)
        settings.set_boolean("background-notification-shown", True)
