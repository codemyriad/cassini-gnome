# SPDX-License-Identifier: GPL-3.0-or-later
"""L-66 (D-1046): the Keyboard shortcuts dialog lists exactly HARMONY section 8.

Parses the GNOME column of the vendored cassini-ux contract and compares it,
row for row and in order, with the accelerators in shortcuts-dialog.ui. Set
CASSINI_UX_DIR to a cassini-ux checkout to compare against it instead.
"""

import ast
import hashlib
import json
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from cassini_talk.shortcuts_table import SUBTITLES, TITLES

ROOT = Path(__file__).resolve().parents[1]
VENDORED = ROOT / "tests/ux"  # pinned by build-aux/check-ux-vectors.py

# Rows the dialog may not list yet, with the decision that keeps them out. Empty since D-1032
# shipped Help (F1) and the in-Library Trash (Delete permanently…); cassini-ux PR 39 (D101) is open.
WITHHELD = set()

# HARMONY rows whose single table cell the dialog shows as several items.
ITEMS_PER_ROW = {
    "Play/Pause; back/forward 10 s": ["play_pause", "skip_back", "skip_forward"],
    "Close window / Quit": ["close_window", "quit"],
}

MODIFIERS = {"Ctrl": "<Control>", "Alt": "<Alt>", "Shift": "<Shift>"}
KEYS = {
    "←": "Left",
    "→": "Right",
    "↑": "Up",
    "↓": "Down",
    "Enter": "Return",
    "Esc": "Escape",
    "Space": "space",
    ",": "comma",
}


def harmony():
    if source := os.environ.get("CASSINI_UX_DIR"):
        return (Path(source) / "contract/HARMONY.md").read_text(encoding="utf-8")
    return (VENDORED / "HARMONY.md").read_text(encoding="utf-8")


def keyboard_rows(markdown):
    section = markdown.split("\n## 8. Keyboard\n", 1)[1].split("\n## 9.", 1)[0]
    rows = []
    for line in section.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if not line.startswith("|") or cells[0] in ("Action", "---"):
            continue
        rows.append((cells[0], cells[1]))
    return rows


def gtk_accelerators(gnome_cell):
    """'Alt+Shift+↓ / Alt+Shift+↑' -> ['<Alt><Shift>Down', '<Alt><Shift>Up']."""
    cell = re.sub(r"\s*\(.*\)\s*$", "", gnome_cell)
    accelerators = []
    for chord in re.split(r"\s*;\s*|\s+/\s+", cell):
        *modifiers, key = chord.split("+")
        key = KEYS.get(key, key.lower() if len(key) == 1 else key)
        accelerators.append("".join(MODIFIERS[m] for m in modifiers) + key)
    return accelerators


def dialog_items():
    tree = ET.parse(ROOT / "src/shortcuts-dialog.ui")
    items = []
    for item in tree.iter("object"):
        if item.get("class") == "AdwShortcutsItem":
            properties = {p.get("name"): (p.text or "") for p in item.findall("property")}
            assert "action-name" not in properties, "List the contract accelerator, not whatever an action has bound"
            items.append((item.get("id"), properties["accelerator"].split()))
    sections = [o for o in tree.iter("object") if o.get("class") == "AdwShortcutsSection"]
    assert len(sections) == 1, "HARMONY section 8 is one table in one order"
    return items


def test_vendored_harmony_matches_its_pin():
    upstream = json.loads((VENDORED / "UPSTREAM.json").read_text())
    digest = hashlib.sha256((VENDORED / "HARMONY.md").read_bytes()).hexdigest()
    assert upstream["sha256"]["HARMONY.md"] == digest, "Refresh tests/ux with check-ux-vectors.py; never edit it here"


def test_dialog_lists_exactly_harmony_section_8_in_order():
    expected = []
    for action, gnome in keyboard_rows(harmony()):
        if action in WITHHELD:
            continue
        accelerators = gtk_accelerators(gnome)
        names = ITEMS_PER_ROW.get(action)
        if names is None:
            expected.append((action, accelerators))
        else:
            assert len(names) == len(accelerators), action
            expected.extend((action, [accelerator]) for accelerator in accelerators)
    items = dialog_items()
    assert [accelerators for _action, accelerators in expected] == [accelerators for _id, accelerators in items]
    assert [name for name, _ in items] == list(TITLES), "Every dialog row has a catalog title, in table order"


def test_withheld_rows_are_still_in_the_contract():
    actions = {action for action, _gnome in keyboard_rows(harmony())}
    assert actions >= WITHHELD and set(ITEMS_PER_ROW) <= actions


def test_titles_come_from_the_shared_catalog():
    catalog = {e["id"]: e["en"] for e in json.loads((ROOT / "src/cassini_talk/strings.json").read_text())["entries"]}
    for identifier in [*TITLES.values(), *SUBTITLES.values()]:
        assert identifier is None or identifier in catalog, identifier
    assert catalog[TITLES["import"]] == "Import audio…" and catalog[TITLES["export"]] == "Export…"


def test_listed_accelerators_are_the_ones_the_app_binds():
    """Each in-app row is really bound; removed shortcuts are not."""
    tree = ast.parse((ROOT / "src/cassini_talk/main.py").read_text())
    bound = set()
    for call in ast.walk(tree):
        if isinstance(call, ast.Call) and getattr(call.func, "attr", "") in ("set_accels_for_action", "_add_action"):
            for argument in call.args:
                if isinstance(argument, ast.List):
                    bound.update(ast.literal_eval(argument))
    bound = {
        re.sub(r"<(\w+)>", lambda m: f"<{'Control' if m[1] == 'primary' else m[1].capitalize()}>", a) for a in bound
    }
    elsewhere = {
        "<Control><Alt>r",  # GlobalShortcuts portal preferred trigger
        "<Alt>Down",
        "<Alt>Up",
        "<Alt><Shift>Down",
        "<Alt><Shift>Up",
        "Escape",
        "<Shift>Return",  # listening (D70)
        "Delete",  # Library row / open meeting; focused Trash row (trash_dialog.py)
    }
    for name, accelerators in dialog_items():
        for accelerator in accelerators:
            assert accelerator in bound or accelerator in elsewhere, (name, accelerator)
    assert not {"<Control>bracketright", "<Control>bracketleft", "<Control>e", "<Control>n"} & bound
    portal = (ROOT / "src/cassini_talk/global_shortcut.py").read_text()
    assert '"CTRL+ALT+r"' in portal
