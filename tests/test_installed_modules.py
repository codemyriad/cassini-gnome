# SPDX-License-Identifier: GPL-3.0-or-later
"""Every product module is installed: a missing entry only fails in packaged builds."""

import re
from pathlib import Path

ROOT = Path(__file__).parents[1]
# Source-tree only, with the reason it is not installed.
NOT_INSTALLED = {"cassini_talk/recording_window.py": "legacy window used only by tests/ui/quit.py"}


def test_every_cassini_talk_module_is_in_the_meson_install_list():
    listed = set(re.findall(r"'(cassini_talk/[^']+\.py)'", (ROOT / "src/meson.build").read_text()))
    present = {
        str(path.relative_to(ROOT / "src"))
        for path in (ROOT / "src/cassini_talk").rglob("*.py")
        if "__pycache__" not in path.parts
    }
    assert present - listed == set(NOT_INSTALLED), "add new modules to src/meson.build"
    assert not listed - present, "src/meson.build installs a module that no longer exists"
