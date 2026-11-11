#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Out-of-process AT-SPI transcript Text/Hypertext activation check."""

import json
import sys

import gi

gi.require_version("Atspi", "2.0")
from gi.repository import Atspi  # noqa: E402

pid, expected = int(sys.argv[1]), sys.argv[2]
root = Atspi.get_desktop(0)
apps = [root.get_child_at_index(i) for i in range(root.get_child_count())]
app = next(a for a in apps if a.get_process_id() == pid)
pending, found, links, roles = [app], None, [], []
while pending:
    node = pending.pop()
    name = node.get_name()
    roles.append((node.get_role_name(), name[:80]))
    if name == expected:
        found = node
        break
    if node.get_role() != Atspi.Role.LIST_ITEM or node.get_state_set().contains(Atspi.StateType.SHOWING):
        pending.extend(node.get_child_at_index(i) for i in range(node.get_child_count()))
assert found, ("Transcript text absent from AT-SPI", expected[:80], roles)
text = found.get_text_iface()
assert text and Atspi.Text.get_text(text, 0, -1) == expected, ("No readable transcript Text", roles)
hypertext = found.get_hypertext_iface()
assert hypertext and hypertext.get_n_links(), ("No transcript Hypertext", roles)
if len(sys.argv) > 3 and sys.argv[3] == "baseline":
    child = found.get_child_at_index(0)
    action = child.get_action_iface() if child else None
    print(
        json.dumps(
            dict(
                text=True,
                links=hypertext.get_n_links(),
                namedWordActions=False,
                nativeLinkAction=bool(action and action.get_n_actions()),
            )
        )
    )
    raise SystemExit(0)
parent = found.get_parent()
for _ in range(4):
    children = [parent.get_child_at_index(i) for i in range(parent.get_child_count())]
    if any(node.get_role() == Atspi.Role.LINK and node.get_name() for node in children):
        break
    parent = parent.get_parent()
child = next((node for node in children if node.get_role() == Atspi.Role.LINK and node.get_name()), None)
assert child, ("No named actionable word", [(c.get_role_name(), c.get_name()) for c in children])
action = child.get_action_iface()
assert action and action.get_n_actions() and action.do_action(0), "Timed-word AT-SPI action failed"
print(
    json.dumps(
        dict(
            text=True,
            links=hypertext.get_n_links(),
            objectRole=child.get_role_name(),
            action=action.get_action_name(0),
            word=child.get_name(),
            roles=roles[-12:],
        )
    )
)
