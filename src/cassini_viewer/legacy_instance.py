# SPDX-License-Identifier: GPL-3.0-or-later
"""D45: activate the old identity's safe upgrade flow before any Library IO."""

from gi.repository import Gio, GLib

NEW = "io.codemyriad.CassiniTalk"
OLD = "com.gocassini.Viewer"
ABSENT = "org.freedesktop.DBus.Error.NameHasNoOwner"
GONE = {ABSENT, "org.freedesktop.DBus.Error.ServiceUnknown"}


def forward(application_id, arguments, *, connection=None):
    if application_id not in (NEW, NEW + ".Devel"):
        return False
    if any(arg in ("-h", "--help", "--help-all", "--help-gtk", "--version") for arg in arguments[1:]):
        return False
    legacy = OLD + (".Devel" if application_id.endswith(".Devel") else "")
    path = "/" + legacy.replace(".", "/")
    bus = connection or Gio.bus_get_sync(Gio.BusType.SESSION, None)
    files = []
    literal = skip = False
    for arg in arguments[1:]:
        if skip:
            skip = False
        elif arg == "--":
            literal = True
        elif not literal and arg in ("--display", "--name", "--class"):
            skip = True
        elif literal or not arg.startswith("-"):
            files.append(Gio.File.new_for_commandline_arg(arg).get_uri())
    intents = []
    if "--cassini-resume-record" in arguments:
        intents.append(("ActivateAction", GLib.Variant("(sava{sv})", ("record", [], {}))))
    if files:
        intents.append(("Open", GLib.Variant("(asa{sv})", (files, {}))))
    if not intents:
        intents.append(("Activate", GLib.Variant("(a{sv})", ({},))))

    def owner():
        try:
            return bus.call_sync(
                "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "GetNameOwner",
                GLib.Variant("(s)", (legacy,)), None, Gio.DBusCallFlags.NONE, 5000, None,
            ).unpack()[0]
        except GLib.Error as error:
            if Gio.DBusError.get_remote_error(error) == ABSENT:
                return None
            raise  # An unknown owner is not permission to open a second writer.

    for method, params in intents:
        for attempt in range(3):
            unique = owner()
            if unique is None:
                return False
            try:
                bus.call_sync(
                    unique, path, "org.freedesktop.Application", method, params,
                    None, Gio.DBusCallFlags.NONE, 5000, None,
                )
                if method == "ActivateAction":
                    # D42 has accepted Record. If it restarts before Open is
                    # delivered, let the new singleton replay only the files.
                    arguments.remove("--cassini-resume-record")
                break
            except GLib.Error as error:
                if Gio.DBusError.get_remote_error(error) not in GONE:
                    raise
        else:
            raise RuntimeError("The previous Cassini instance is changing; retry activation")
    # D42 retains the intent and restarts when capture/preparation is safe.
    # A pre-D42 release stays the sole writer until it is closed.
    return True
