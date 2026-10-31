# SPDX-License-Identifier: GPL-3.0-or-later
"""D41 lifetime/notification rules, independent of a desktop's portal support."""

from types import SimpleNamespace

import pytest

pytest.importorskip("gi.repository.Gio")
from cassini_viewer import windowless
from cassini_viewer.windowless import WindowlessBackground


class Portal:
    def __init__(self):
        self.granted = False
        self.callback = None
        self.statuses = []
        self.requests = 0
        self.listed = True
        self.status_supported = True

    def request(self, callback):
        self.callback = callback
        self.requests += 1

    def finish(self, allowed):
        self.granted = allowed
        callback, self.callback = self.callback, None
        callback(allowed)

    def status(self, message, callback=None):
        self.statuses.append(message)
        if not self.listed:
            self.status_supported = False
        if callback:
            callback(self.listed)


@pytest.fixture
def notification_body(monkeypatch):
    def new(title):
        value = SimpleNamespace(title=title, body=None, default_action=None)
        value.set_body = lambda body: setattr(value, "body", body)
        value.set_default_action = lambda action: setattr(value, "default_action", action)
        return value

    monkeypatch.setattr(windowless, "Gio", SimpleNamespace(Notification=SimpleNamespace(new=new)))


def application():
    settings = {}
    notifications = []
    window = SimpleNamespace(visible=True)
    window.get_visible = lambda: window.visible
    app = SimpleNamespace(
        _auto_recording_held=True,
        _quitting=False,
        in_flatpak=True,
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


def test_host_status_rejection_uses_launcher_without_claiming_background_apps(notification_body):
    app, window, notifications = application()
    app.in_flatpak = False
    window.visible = False
    app.background_recording.listed = False
    presence = WindowlessBackground(app)
    presence.refresh()
    app.background_recording.finish(True)
    assert len(notifications) == 1
    assert notifications[0][1].body == ("It records browser meetings automatically. To open it, start Cassini again.")
    assert app.recording_settings.get_boolean("background-notification-shown")


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
        app.status_notifier.label = values["label"]

    app.status_notifier = SimpleNamespace(recording=False, message="", label="", update=update)
    recorder = SimpleNamespace(pipeline=object())
    app.native_captures = {"one": recorder}
    presence = WindowlessBackground(app)
    presence.refresh("Recording, 00:12", "00:12")
    assert updates[-1] == dict(
        recording=True, message="Recording, 00:12", label="00:12", windowless=False, automatic=True
    )
    assert not app.background_recording.requests
    presence.refresh()  # Activation/level refresh before the next timer tick.
    assert updates[-1]["recording"] and updates[-1]["message"] == "Recording, 00:12"
    assert updates[-1]["label"] == "00:12", "the panel timer must not blank between ticks"
    app._quitting = True
    presence.refresh()
    assert updates[-1]["recording"], "Capture must stay visible until audio finishes"
    app.native_captures = {}
    presence.refresh()
    assert not updates[-1]["recording"] and not updates[-1]["automatic"] and updates[-1]["label"] == ""


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


def test_accepted_indicator_names_the_real_top_bar_route(notification_body):
    app, window, notifications = application()
    app.in_flatpak = False
    window.visible = False
    app.background_recording.listed = False
    app.status_notifier = SimpleNamespace(
        available=True, registering=False, recording=False, message="", update=lambda **_: None
    )
    presence = WindowlessBackground(app)
    presence.refresh()
    app.background_recording.finish(True)
    assert notifications[0][1].body == (
        "It records browser meetings automatically. To open it, use Cassini’s icon in the top bar."
    )
    assert notifications[0][1].default_action == "app.open-recording"
    presence.indicator_ready()
    assert len(notifications) == 1


@pytest.mark.parametrize("accepted", [True, False])
def test_pending_indicator_chooses_its_route_only_after_registration_finishes(notification_body, accepted):
    app, window, notifications = application()
    app.in_flatpak = False
    window.visible = False
    app.background_recording.listed = False
    indicator = app.status_notifier = SimpleNamespace(
        available=False, registering=True, recording=False, message="", update=lambda **_: None
    )
    presence = WindowlessBackground(app)
    presence.refresh()
    app.background_recording.finish(True)
    assert not notifications
    indicator.registering, indicator.available = False, accepted
    presence.indicator_ready()
    assert len(notifications) == 1
    assert notifications[0][1].body.endswith(
        "use Cassini’s icon in the top bar." if accepted else "start Cassini again."
    )


def test_pending_portal_status_never_causes_a_premature_launcher_notice(notification_body):
    app, window, notifications = application()
    window.visible = False
    presence = WindowlessBackground(app)
    presence.refresh()
    app.background_recording.granted = True
    presence._published(False)  # Duplicate call while the real SetStatus reply is pending.
    assert not notifications and app.background_recording.status_supported
    presence._published(True)
    assert notifications[0][1].body.endswith("use Background Apps in the system menu.")


def test_late_indicator_after_reopening_does_not_send_a_hidden_notice(notification_body):
    app, window, notifications = application()
    app.in_flatpak = False
    window.visible = False
    app.background_recording.listed = False
    indicator = app.status_notifier = SimpleNamespace(
        available=False, registering=True, recording=False, message="", update=lambda **_: None
    )
    presence = WindowlessBackground(app)
    presence.refresh()
    app.background_recording.finish(True)
    window.visible = True
    presence.refresh()
    indicator.registering, indicator.available = False, True
    presence.indicator_ready()
    assert not notifications
    window.visible = False
    presence.refresh()
    assert notifications[0][1].body.endswith("use Cassini’s icon in the top bar.")


def test_native_never_claims_background_apps_even_if_status_call_succeeds(notification_body):
    app, window, notifications = application()
    app.in_flatpak = False
    window.visible = False
    presence = WindowlessBackground(app)
    presence.refresh()
    app.background_recording.finish(True)
    assert notifications[0][1].body.endswith("start Cassini again.")
    assert "Background Apps" not in notifications[0][1].body


def test_indicator_registration_waits_for_background_permission_before_notifying(notification_body):
    app, window, notifications = application()
    app.in_flatpak = False
    window.visible = False
    app.status_notifier = SimpleNamespace(
        available=True, registering=False, recording=False, message="", update=lambda **_: None
    )
    presence = WindowlessBackground(app)
    presence.refresh()
    presence.indicator_ready()
    assert not notifications, "the portal answer comes first"
    app.background_recording.finish(True)
    assert len(notifications) == 1 and not window.visible


@pytest.mark.parametrize("registering", [False, True])
def test_native_indicator_stays_windowless_when_the_portal_is_absent_or_fails(notification_body, registering):
    app, window, notifications = application()
    app.in_flatpak = False
    window.visible = False
    indicator = app.status_notifier = SimpleNamespace(
        available=not registering, registering=registering, recording=False, message="", update=lambda **_: None
    )
    presence = WindowlessBackground(app)
    presence.refresh()
    app.background_recording.finish(False)  # No portal, no Background, or a failed request.
    if registering:
        assert not window.visible and not notifications
        indicator.registering, indicator.available = False, True
        presence.indicator_ready()
    assert not window.visible, "an accepted top-bar item keeps a native app reachable"
    assert notifications[0][1].body.endswith("use Cassini’s icon in the top bar.")
    presence.refresh()
    presence.indicator_ready()
    assert len(notifications) == 1 and app.background_recording.requests == 1 and not window.visible


def test_native_without_an_accepted_indicator_reopens_when_the_portal_fails():
    app, window, notifications = application()
    app.in_flatpak = False
    window.visible = False
    indicator = app.status_notifier = SimpleNamespace(
        available=False, registering=True, recording=False, message="", update=lambda **_: None
    )
    presence = WindowlessBackground(app)
    presence.refresh()
    app.background_recording.finish(False)
    assert not window.visible, "wait for the host's answer"
    indicator.registering = False
    presence.indicator_ready()
    assert window.visible and not notifications


def test_flatpak_keeps_portal_enforcement_even_with_an_indicator():
    app, window, notifications = application()
    window.visible = False
    app.status_notifier = SimpleNamespace(
        available=True, registering=False, recording=False, message="", update=lambda **_: None
    )
    presence = WindowlessBackground(app)
    presence.refresh()
    app.background_recording.finish(False)
    assert window.visible and not notifications
