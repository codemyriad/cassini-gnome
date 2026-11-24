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
# window then refuses to close. A retry a few frames later works; 1.8 has no
# such window.
CLOSE_GRACE_MS = 1000
FORCE_CLOSE_RETRY_MS = 16


def install_dialog_close_fix(version=None):
    """Make ``Adw.Dialog.close()``/``force_close()`` finish on libadwaita < 1.8 (D-1140).

    ``force_close()`` that has not emitted "closed" is retried every
    :data:`FORCE_CLOSE_RETRY_MS` until it does or the dialog leaves its window. A ``close()``
    libadwaita accepted (returned True) that has not emitted "closed" within
    :data:`CLOSE_GRACE_MS`, past the closing animation, becomes a
    ``force_close()``. Returns whether it installed.
    """
    from gi.repository import GLib

    if version is None:
        version = (Adw.get_major_version(), Adw.get_minor_version())
    if tuple(version) >= (1, 8) or getattr(Adw.Dialog.close, "_cassini_close_fix", False):
        return False
    original_close, original_force_close = Adw.Dialog.close, Adw.Dialog.force_close

    def watch(dialog):
        state = {"closed": False}
        state["handler"] = dialog.connect("closed", lambda *_: state.update(closed=True))
        return state

    def force_close(self):
        state = watch(self)
        original_force_close(self)
        if state["closed"]:
            self.disconnect(state["handler"])
            return

        def retry():
            # Until it closes or leaves its window (destroyed with it). A timer,
            # not a tick callback: a hidden window's dialog must close too.
            if not state["closed"] and self.get_parent() is not None:
                original_force_close(self)
            if state["closed"] or self.get_parent() is None:
                self.disconnect(state["handler"])
                return GLib.SOURCE_REMOVE
            return GLib.SOURCE_CONTINUE

        GLib.timeout_add(FORCE_CLOSE_RETRY_MS, retry)

    def close(self):
        state = watch(self)
        accepted = original_close(self)
        if not accepted or state["closed"]:
            self.disconnect(state["handler"])
            return accepted

        def finish():
            self.disconnect(state["handler"])
            if not state["closed"]:
                force_close(self)
            return GLib.SOURCE_REMOVE

        GLib.timeout_add(CLOSE_GRACE_MS, finish)
        return accepted

    close._cassini_close_fix = True
    Adw.Dialog.close, Adw.Dialog.force_close = close, force_close
    return True
