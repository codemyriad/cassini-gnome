# Copyright 2026 Codemyriad OÜ
# SPDX-License-Identifier: GPL-3.0-or-later
"""Dispatch real GStreamer playback messages until the expected event."""


def wait_for_playback(predicate, event):
    from gi.repository import GLib

    context = GLib.MainContext.default()
    expired = False

    def expire(*_args):
        nonlocal expired
        expired = True
        return GLib.SOURCE_REMOVE

    guard = GLib.timeout_source_new(10_000)
    guard.set_callback(expire)
    guard.attach(context)
    try:
        while not predicate() and not expired:
            context.iteration(True)
        assert predicate(), f"Playback did not deliver {event}"
    finally:
        guard.destroy()
