#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Read the real Chromium address field; never type or navigate via a debug API."""

import json
import os
import sys
import time

if os.environ.get("CASSINI_ISOLATED_AUDIO") != "1" or os.environ.get("WAYLAND_DISPLAY"):
    raise SystemExit("Private display required")
import gi

gi.require_version("Atspi", "2.0")
from gi.repository import Atspi, GLib  # noqa: E402

Atspi.set_timeout(500, 500)
expected = sys.argv[1] if len(sys.argv) > 1 else None


def walk(node, depth=0):
    if depth > 16:
        return
    try:
        yield node
        for index in range(node.get_child_count()):
            child = node.get_child_at_index(index)
            if child:
                yield from walk(child, depth + 1)
    except GLib.Error:
        return


deadline = time.monotonic() + 3
last = None
focused_nodes = []
listener = Atspi.EventListener.new(lambda *_: None)
listener.register("object:state-changed")
while time.monotonic() < deadline:
    context = GLib.MainContext.default()
    for _ in range(64):
        if not context.pending():
            break
        context.iteration(False)
    for node in walk(Atspi.get_desktop(0)):
        try:
            states = node.get_state_set()
            if not states.contains(Atspi.StateType.FOCUSED):
                continue
            application = node.get_application().get_name()
            focused_nodes.append((application, node.get_role_name(), node.get_name()))
            if not any(name in application.lower() for name in ("chrom", "brave", "edge")):
                continue
            if node.get_name() != "Address and search bar":
                continue
            interface = node.get_text_iface()
            if interface is None:
                continue
            value = Atspi.Text.get_text(interface, 0, Atspi.Text.get_character_count(interface))
            last = dict(focused=True, application=application, name=node.get_name(), value=value)
            if expected is None or value.rstrip("/") == expected.rstrip("/"):
                print(json.dumps(dict(ready=True, **last)))
                raise SystemExit(0)
        except (GLib.Error, AttributeError):
            continue
    time.sleep(0.025)
print(
    json.dumps(
        dict(
            ready=False,
            observed=last,
            focused=focused_nodes[-12:],
            applications=[
                child.get_name()
                for child in (
                    Atspi.get_desktop(0).get_child_at_index(i) for i in range(Atspi.get_desktop(0).get_child_count())
                )
            ],
        )
    )
)
