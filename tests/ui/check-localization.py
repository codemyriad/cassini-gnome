#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Assert real mapped copy and gettext menu labels from the installed launcher."""

import json
import re
import struct
import sys
from pathlib import Path

shot, lang = Path(sys.argv[1]), sys.argv[2]
catalog = {
    e["id"]: e for e in json.loads((Path(__file__).parents[2] / "src/cassini_talk/strings.json").read_text())["entries"]
}
state = json.loads(shot.with_suffix(".json").read_text())
assert state["uiLanguage"] == lang, state
assert state["lang"] == "en_GB.UTF-8", "UI selection must not rewrite the formatting locale"
assert struct.unpack(">II", shot.read_bytes()[16:24]) == (1120, 720)
for identifier in ("firstRun.title", "firstRun.body", "common.record"):
    expected = catalog[identifier]["l10n"][lang]["text"]
    assert expected in state["visibleLabels"], (identifier, expected, state["visibleLabels"])
assert {"fr": "Ouvrir…", "pt-BR": "Abrir…"}[lang] in state["menuLabels"], state["menuLabels"]
log = shot.with_suffix(".log").read_text()
assert not re.search(r"(?:Gtk|Adw|GLib-GObject)-(?:WARNING|CRITICAL)|Traceback \(most recent call last\)", log), log
print(f"PASS real {lang} launch: mapped catalog copy, gettext menu, unchanged formatting locale, no GTK diagnostics")
