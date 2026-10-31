# SPDX-License-Identifier: GPL-3.0-or-later
"""Background portal request on desktops whose portal predates the host Registry."""

from types import SimpleNamespace

import pytest

pytest.importorskip("gi.repository.Gio")
from gi.repository import Gio, GLib

from cassini_viewer import background_recording
from cassini_viewer.background_recording import BackgroundRecording


class Delegate:
    def __init__(self, module, **overrides):
        self._module, self._overrides = module, overrides

    def __getattr__(self, name):
        return self._overrides[name] if name in self._overrides else getattr(self._module, name)


class Bus:
    def __init__(self, register_error=None):
        self.register_error = register_error
        self.sync_calls, self.calls, self.closed = [], [], False

    def call_sync(self, _service, _path, interface, method, *_args):
        self.sync_calls.append((interface, method))
        if self.register_error:
            raise self.register_error

    def get_unique_name(self):
        return ":1.42"

    def signal_subscribe(self, *_args):
        return 7

    def call(self, _service, _path, interface, method, *_args):
        self.calls.append((interface, method))

    def close(self, *_args):
        self.closed = True


def request(monkeypatch, bus, *, native=True):
    """Run request() with the worker thread and idle callbacks inline."""

    class Thread:
        def __init__(self, target, **_kwargs):
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(background_recording, "threading", SimpleNamespace(Thread=Thread))
    monkeypatch.setattr(
        background_recording,
        "GLib",
        Delegate(GLib, idle_add=lambda function, *args: function(*args), timeout_add_seconds=lambda *_: 1),
    )
    connection = Delegate(Gio.DBusConnection, new_for_address_sync=lambda *_: bus)
    monkeypatch.setattr(
        background_recording,
        "Gio",
        Delegate(Gio, dbus_address_get_for_bus_sync=lambda *_: "unix:path=/nonexistent", DBusConnection=connection),
    )
    results = []
    portal = BackgroundRecording("io.codemyriad.CassiniTalk", native=native)
    portal.request(results.append)
    return portal, results


def test_portal_without_host_registry_still_requests_background(monkeypatch):
    missing = GLib.Error.new_literal(
        Gio.dbus_error_quark(), "No such interface “org.freedesktop.host.portal.Registry”", Gio.DBusError.UNKNOWN_METHOD
    )
    bus = Bus(register_error=missing)
    portal, results = request(monkeypatch, bus)
    assert bus.sync_calls == [("org.freedesktop.host.portal.Registry", "Register")]
    assert bus.calls == [("org.freedesktop.portal.Background", "RequestBackground")]
    assert results == [] and portal.callback is not None, "the portal's Response decides, not Register"
    assert portal.bus is bus and not bus.closed


def test_registered_native_app_and_flatpak_request_background(monkeypatch):
    for native in (True, False):
        bus = Bus()
        _portal, results = request(monkeypatch, bus, native=native)
        assert bus.sync_calls == ([("org.freedesktop.host.portal.Registry", "Register")] if native else [])
        assert bus.calls == [("org.freedesktop.portal.Background", "RequestBackground")] and results == []


def test_cancelled_registration_still_fails_the_request():
    cancellable = Gio.Cancellable()
    cancellable.cancel()
    error = GLib.Error.new_literal(Gio.io_error_quark(), "Operation was cancelled", Gio.IOErrorEnum.CANCELLED)
    portal = BackgroundRecording("io.codemyriad.CassiniTalk", native=True)
    with pytest.raises(GLib.Error):
        portal._register(Bus(register_error=error), cancellable)
