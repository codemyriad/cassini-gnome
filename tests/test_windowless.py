# SPDX-License-Identifier: GPL-3.0-or-later
"""D41 lifetime/notification rules, independent of a desktop's portal support."""

from types import SimpleNamespace

import pytest

pytest.importorskip("gi.repository.Gio")
from cassini_viewer.windowless import WindowlessBackground


class Portal:
    def __init__(self):
        self.granted = False
        self.callback = None
        self.statuses = []
        self.requests = 0
        self.listed = True

    def request(self, callback):
        self.callback = callback
        self.requests += 1

    def finish(self, allowed):
        self.granted = allowed
        callback, self.callback = self.callback, None
        callback(allowed)

    def status(self, message, callback=None):
        self.statuses.append(message)
        if callback:
            callback(self.listed)


def application():
    settings = {}
    notifications = []
    window = SimpleNamespace(visible=True)
    window.get_visible = lambda: window.visible
    app = SimpleNamespace(
        _auto_recording_held=True,
        _quitting=False,
        get_windows=lambda: [window],
        background_recording=Portal(),
        recording_settings=SimpleNamespace(
            get_boolean=lambda name: settings.get(name, False),
            set_boolean=lambda name, value: settings.update({name: value}),
        ),
        send_notification=lambda identifier, value: notifications.append((identifier, value)),
        activate=lambda: setattr(window, "visible", True),
    )
    return app, window, notifications


def test_armed_close_requests_once_and_notification_is_once_ever():
    app, window, notifications = application()
    presence = WindowlessBackground(app)
    presence.refresh()
    assert app.background_recording.requests == 0
    window.visible = False
    presence.refresh()
    presence.refresh()
    assert app.background_recording.requests == 1 and not notifications
    app.background_recording.finish(True)
    assert app.background_recording.statuses[-1] == "Ready to record browser meetings"
    assert len(notifications) == 1
    window.visible = True
    presence.refresh()
    assert app.background_recording.statuses[-1] == ""
    window.visible = False
    WindowlessBackground(app).refresh()  # Same persisted preference, new manager.
    assert len(notifications) == 1 and app.background_recording.requests == 1


def test_recording_status_takes_precedence_and_idle_begins_after_stop():
    app, window, notifications = application()
    window.visible = False
    presence = WindowlessBackground(app)
    presence.refresh("Recording, 00:12")
    app.background_recording.finish(True)
    assert app.background_recording.statuses[-1] == "Recording, 00:12"
    assert not notifications
    presence.refresh()
    assert app.background_recording.statuses[-1] == "Ready to record browser meetings"
    assert len(notifications) == 1
    app._quitting = True
    presence.refresh()
    assert app.background_recording.statuses[-1] == ""


def test_denied_background_request_reopens_a_window():
    app, window, notifications = application()
    window.visible = False
    presence = WindowlessBackground(app)
    presence.refresh()
    app.background_recording.finish(False)
    assert window.visible and not notifications


def test_host_status_rejection_never_claims_background_apps_can_open_it():
    app, window, notifications = application()
    window.visible = False
    app.background_recording.listed = False
    presence = WindowlessBackground(app)
    presence.refresh()
    app.background_recording.finish(True)
    assert not notifications
    assert not app.recording_settings.get_boolean("background-notification-shown")


def test_late_permission_after_reopen_does_not_send_hidden_notification():
    app, window, notifications = application()
    window.visible = False
    presence = WindowlessBackground(app)
    presence.refresh()
    window.visible = True
    presence.refresh()
    app.background_recording.finish(True)
    assert app.background_recording.statuses[-1] == "" and not notifications


def test_unarmed_windowless_app_does_not_claim_ready_to_record():
    app, window, notifications = application()
    app._auto_recording_held = False
    window.visible = False
    WindowlessBackground(app).refresh()
    assert not app.background_recording.requests and not notifications
    assert app.background_recording.statuses[-1] == ""


def test_indicator_remains_visible_for_recording_with_library_open():
    app, window, _ = application()
    updates = []

    def update(**values):
        updates.append(values)
        app.status_notifier.recording = values["recording"]
        app.status_notifier.message = values["message"]

    app.status_notifier = SimpleNamespace(recording=False, message="", update=update)
    recorder = SimpleNamespace(pipeline=object())
    app.native_captures = {"one": recorder}
    presence = WindowlessBackground(app)
    presence.refresh("Recording, 00:12")
    assert updates[-1] == dict(recording=True, message="Recording, 00:12", windowless=False, automatic=True)
    assert not app.background_recording.requests
    presence.refresh()  # Activation/level refresh before the next timer tick.
    assert updates[-1]["recording"] and updates[-1]["message"] == "Recording, 00:12"
    app._quitting = True
    presence.refresh()
    assert updates[-1]["recording"], "Capture must stay visible until audio finishes"
    app.native_captures = {}
    presence.refresh()
    assert not updates[-1]["recording"] and not updates[-1]["automatic"]


def test_idle_indicator_only_runs_when_automatic_recording_is_armed_and_windowless():
    app, window, _ = application()
    updates = []
    app.status_notifier = SimpleNamespace(recording=False, message="", update=lambda **values: updates.append(values))
    presence = WindowlessBackground(app)
    presence.refresh()
    assert not updates[-1]["windowless"]
    window.visible = False
    presence.refresh()
    assert updates[-1]["windowless"] and updates[-1]["automatic"] and not updates[-1]["recording"]
    app._auto_recording_held = False
    presence.refresh()
    assert not updates[-1]["automatic"] and not updates[-1]["recording"]
