# SPDX-License-Identifier: GPL-3.0-or-later
"""D45 routes launch intent without entering a second Library writer."""

from pathlib import Path

import pytest

pytest.importorskip("gi.repository.Gio")
from gi.repository import Gio, GLib

from cassini_viewer.legacy_instance import ABSENT, NEW, OLD, forward


class Bus:
    def __init__(self, owners=(":1.42",), failure=None):
        self.owners = iter(owners)
        self.calls = []
        self.failure = failure

    def call_sync(self, destination, path, interface, method, parameters, *unused):
        self.calls.append((destination, path, interface, method, parameters.unpack()))
        if method == "GetNameOwner":
            owner = next(self.owners)
            if owner is None:
                raise Gio.DBusError.new_for_dbus_error(ABSENT, "gone")
            return GLib.Variant("(s)", (owner,))
        if self.failure:
            raise Gio.DBusError.new_for_dbus_error(self.failure, "fixture")
        return GLib.Variant("()", ())


@pytest.mark.parametrize("suffix", ["", ".Devel"])
def test_existing_identity_receives_activation_and_new_services_must_not_start(suffix):
    bus = Bus()
    assert forward(NEW + suffix, ["cassini-talk"], connection=bus)
    assert bus.calls[0][-1] == (OLD + suffix,)
    assert bus.calls[1] == (
        ":1.42", "/" + (OLD + suffix).replace(".", "/"),
        "org.freedesktop.Application", "Activate", ({},),
    )


def test_absent_owner_allows_normal_new_singleton_start():
    assert not forward(NEW, ["cassini-talk"], connection=Bus((None,)))


def test_file_activation_preserves_spaces_and_literal_leading_dash(tmp_path):
    bus = Bus()
    file = tmp_path / "meeting with spaces.opus"
    assert forward(NEW, ["cassini-talk", "--display", ":99", str(file), "--", "-literal.opus"], connection=bus)
    assert bus.calls[1][3:] == ("Open", ([file.as_uri(), Path("-literal.opus").resolve().as_uri()], {}))


def test_owner_exit_during_forwarding_rechecks_before_allowing_start():
    bus = Bus((":1.42", None), failure="org.freedesktop.DBus.Error.ServiceUnknown")
    assert not forward(NEW, ["cassini-talk"], connection=bus)
    assert [call[3] for call in bus.calls] == ["GetNameOwner", "Activate", "GetNameOwner"]


def test_unresponsive_or_inaccessible_owner_never_allows_a_second_writer():
    bus = Bus(failure="org.freedesktop.DBus.Error.NoReply")
    with pytest.raises(GLib.Error):
        forward(NEW, ["cassini-talk"], connection=bus)


def test_record_accepted_before_a_restart_is_not_replayed_but_files_are_retained():
    bus = Bus((":1.42", None))
    args = ["cassini-talk", "--cassini-resume-record", "meeting.opus"]
    assert not forward(NEW, args, connection=bus)
    assert bus.calls[1][3:] == ("ActivateAction", ("record", [], {}))
    assert args == ["cassini-talk", "meeting.opus"]


def test_help_never_activates_an_existing_app():
    bus = Bus()
    assert not forward(NEW, ["cassini-talk", "--help"], connection=bus)
    assert not bus.calls
