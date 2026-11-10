#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Bounded AT-SPI inspection of the private spike desktop, synthetic text only."""

import json

import gi

gi.require_version("Atspi", "2.0")
from gi.repository import Atspi  # noqa: E402

desktop = Atspi.get_desktop(0)
observed, visited = [], 0
pending = [desktop.get_child_at_index(i) for i in range(desktop.get_child_count())]
while pending and visited < 250:
    node = pending.pop()
    if node is None:
        continue
    visited += 1
    try:
        role, name = node.get_role_name(), node.get_name()
        observed.append(
            dict(
                role=role,
                name=name,
                actions=node.get_action_iface().get_n_actions() if node.get_action_iface() else 0,
            )
        )
        if "seek to" in (name or ""):
            break
        # Depth first in document order, with a hard cap. Never enumerate an
        # entire 50k-word accessibility tree just to prove one button works.
        pending.extend(node.get_child_at_index(i) for i in reversed(range(min(node.get_child_count(), 80))))
    except Exception as error:
        observed.append(dict(error=str(error)))
print(json.dumps(dict(visited=visited, observed=observed)), flush=True)
