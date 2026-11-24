# SPDX-License-Identifier: GPL-3.0-or-later
"""Fallbacks for libadwaita widgets newer than libadwaita 1.5 (D-1140).

Ubuntu 24.04 LTS has libadwaita 1.5 and GTK 4.14. The app uses a few newer
widgets; on older libraries each helper here builds the closest 1.5 widget,
with the same calls the callers make (append, remove, get_first_child ...).
"""

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # noqa: E402


def wrap_box(*, child_spacing, line_spacing, **properties):
    """``Adw.WrapBox`` (libadwaita 1.7), else a non-selectable ``Gtk.FlowBox``."""
    if hasattr(Adw, "WrapBox"):
        return Adw.WrapBox(child_spacing=child_spacing, line_spacing=line_spacing, **properties)
    box = Gtk.FlowBox(
        column_spacing=child_spacing,
        row_spacing=line_spacing,
        selection_mode=Gtk.SelectionMode.NONE,
        homogeneous=False,
        min_children_per_line=1,
        max_children_per_line=1000,
        valign=Gtk.Align.START,
        **properties,
    )
    box.add_css_class("cassini-wrap-box")
    return box


def has_shortcuts_dialog():
    """Whether libadwaita has ``Adw.ShortcutsDialog`` (1.8)."""
    return hasattr(Adw, "ShortcutsDialog")


# libadwaita 1.5 ignores AdwDialog.close() and force_close() in the first
# frames after present(): "closed" never comes, the dialog stays, and its
# window then refuses to close. Calling it again a frame or two later works
# (and a repeated call during a normal close changes nothing); 1.8 has no such
# window.
RETRY_MS = 16
# A close() still unfinished after this long (well past the closing
# animation) is forced.
CLOSE_GRACE_MS = 2000


def install_dialog_close_fix(version=None):
    """Make ``Adw.Dialog.close()``/``force_close()`` finish on libadwaita < 1.8 (D-1140).

    Until "closed" comes or the dialog leaves its window, an accepted
    ``close()`` (it returned True) and a ``force_close()`` are repeated every
    :data:`RETRY_MS`; a ``close()`` unfinished after :data:`CLOSE_GRACE_MS`
    becomes a ``force_close()``. Timers, not tick callbacks, so the dialog of
    a hidden window closes too. Returns whether it installed.
    """
    from gi.repository import GLib

    if version is None:
        version = (Adw.get_major_version(), Adw.get_minor_version())
    if tuple(version) >= (1, 8) or getattr(Adw.Dialog.close, "_cassini_close_fix", False):
        return False
    original_close, original_force_close = Adw.Dialog.close, Adw.Dialog.force_close

    def repeat(dialog, call):
        """Call *call* now and every RETRY_MS until "closed"; return its first result."""
        state = {"closed": False, "forced": call is original_force_close, "waited": 0}
        handler = dialog.connect("closed", lambda *_: state.update(closed=True))
        result = call(dialog)
        if state["closed"] or result is False:
            dialog.disconnect(handler)
            return result

        def retry():
            if not state["closed"] and dialog.get_parent() is not None:
                state["waited"] += RETRY_MS
                if not state["forced"] and state["waited"] >= CLOSE_GRACE_MS:
                    state["forced"] = True
                original = original_force_close if state["forced"] else original_close
                original(dialog)
            if state["closed"] or dialog.get_parent() is None:
                dialog.disconnect(handler)
                return GLib.SOURCE_REMOVE
            return GLib.SOURCE_CONTINUE

        GLib.timeout_add(RETRY_MS, retry)
        return result

    def close(self):
        return repeat(self, original_close)

    def force_close(self):
        repeat(self, original_force_close)

    close._cassini_close_fix = True
    Adw.Dialog.close, Adw.Dialog.force_close = close, force_close
    return True
