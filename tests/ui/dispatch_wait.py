# SPDX-License-Identifier: GPL-3.0-or-later
"""Observe GTK state while dispatching actual main-context events."""

from gi.repository import GLib


def dispatch_until(check, message="The GTK operation did not complete"):
    context = GLib.MainContext.default()
    expired = False

    def expire(*_args):
        nonlocal expired
        expired = True
        return GLib.SOURCE_REMOVE

    guard = GLib.timeout_source_new_seconds(18)
    guard.set_callback(expire)
    guard.attach(context)
    try:
        while not check() and not expired:
            context.iteration(True)
        assert check(), message
    finally:
        guard.destroy()
