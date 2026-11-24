# SPDX-License-Identifier: GPL-3.0-or-later
"""L-66: catalog titles for the Keyboard shortcuts dialog, in HARMONY section 8 order.

The rows and accelerators live in shortcuts-dialog.ui; this maps each item id
to its shared-catalog title, and GROUPS gives the two titled groups (D107 §2). ``None`` marks a row whose title is missing from
the cassini-ux catalog (flagged on D-1046), so it keeps the gettext title.
"""

TITLES = {
    "record": "common.record",
    "record_anywhere": "settings.recording.shortcut.title",
    "find": "meeting.find.placeholder",
    "import": "import.action",
    "export": "common.exportEllipsis",
    "preferences": "settings.title",
    "play_pause": None,
    "skip_back": "player.back10",
    "skip_forward": "player.forward10",
    "listen_turns": "shortcuts.listen.turns",
    "listen_speakers": "shortcuts.listen.speakers",
    "play_turn": "shortcuts.playTurn",
    "listen_stop": "listen.stop",
    "back_library": "common.backToLibrary",
    "close_window": "common.closeWindow",
    "quit": "hidden.menu.quit",
    "move_to_trash": "common.moveToTrash",
    "delete_permanently": "trash.deletePermanently",
    "help": "menu.help",
}

# Ctrl+F is one row: Find in this meeting when a meeting is open, sidebar search otherwise (L-44).
SUBTITLES = {"find": "search.field.placeholder"}

# D107 §2 (HARMONY §8): two titled groups, each in table order. Same rows and keys as TITLES.
GROUPS = {
    "general": (
        "shortcuts.group.general",
        (
            "record",
            "record_anywhere",
            "find",
            "import",
            "export",
            "preferences",
            "back_library",
            "close_window",
            "quit",
            "move_to_trash",
            "delete_permanently",
            "help",
        ),
    ),
    "listening": (
        "shortcuts.group.listening",
        ("play_pause", "skip_back", "skip_forward", "listen_turns", "listen_speakers", "play_turn", "listen_stop"),
    ),
}


def sections(ui_text):
    """``[(section id, [(item id, accelerator), ...]), ...]`` from shortcuts-dialog.ui.

    libadwaita 1.5 (Ubuntu 24.04) has no AdwShortcutsDialog, so the app builds
    its own dialog from the same rows (D-1140).
    """
    import xml.etree.ElementTree as ET

    out = []
    for section in ET.fromstring(ui_text).iter("object"):
        if section.get("class") != "AdwShortcutsSection":
            continue
        items = []
        for item in section.iter("object"):
            if item.get("class") == "AdwShortcutsItem":
                accelerator = next(
                    (prop.text or "" for prop in item.findall("property") if prop.get("name") == "accelerator"), ""
                )
                items.append((item.get("id"), accelerator))
        out.append((section.get("id"), items))
    return out
