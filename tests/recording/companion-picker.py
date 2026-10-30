#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Accept only Chromium's visible native extension picker in a private test display."""

import json
import os
import sys
import time

if os.environ.get("CASSINI_ISOLATED_AUDIO") != "1" or os.environ.get("WAYLAND_DISPLAY"):
    raise SystemExit("Private display required")
import gi

gi.require_version("Atspi", "2.0")
from gi.repository import Atspi, GLib  # noqa: E402

Atspi.set_timeout(1000, 1000)
folder = sys.argv[1]
last = []


def walk(root, depth=0):
    if depth > 16:
        return
    try:
        yield root
        for index in range(root.get_child_count()):
            child = root.get_child_at_index(index)
            if child:
                yield from walk(child, depth + 1)
    except GLib.Error:
        return


def picker():
    return next(
        (node for node in walk(Atspi.get_desktop(0)) if node.get_name() == "Select the extension directory."), None
    )


end = time.monotonic() + 10
activated = False
while time.monotonic() < end:
    context = GLib.MainContext.default()
    for _ in range(64):
        if not context.pending():
            break
        context.iteration(False)
    dialog = picker()
    if not dialog and activated:
        print(json.dumps(dict(accepted=True, folder=folder)))
        raise SystemExit(0)
    if dialog:
        nodes = list(walk(dialog))
        last = [(node.get_role_name(), node.get_name()) for node in nodes]
        if not activated:
            entries = []
            for node in nodes:
                try:
                    entry = node.get_text_iface()
                    if entry is None:
                        continue
                    entries.append(Atspi.Text.get_text(entry, 0, Atspi.Text.get_character_count(entry)))
                except (GLib.Error, AttributeError):
                    pass
            if any(value.rstrip("/") == folder.rstrip("/") for value in entries):
                button = next(
                    (node for node in nodes if node.get_role() == Atspi.Role.PUSH_BUTTON and node.get_name() == "Open"),
                    None,
                )
                if button and button.get_state_set().contains(Atspi.StateType.SENSITIVE):
                    assert button.get_action_iface().do_action(0)
                    activated = True
    time.sleep(0.05)
print(json.dumps(dict(accepted=False, activated=activated, nodes=last)))
raise SystemExit("Native extension directory picker did not accept the copied folder")
