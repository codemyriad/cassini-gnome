# SPDX-License-Identifier: GPL-3.0-or-later
"""L-66: catalog titles for the Keyboard shortcuts dialog, in HARMONY section 8 order.

The rows and accelerators live in shortcuts-dialog.ui; this maps each item id
to its shared-catalog title. ``None`` marks a row whose title is missing from
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
