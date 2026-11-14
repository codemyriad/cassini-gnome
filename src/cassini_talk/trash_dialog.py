# SPDX-License-Identifier: GPL-3.0-or-later
"""☰ › Trash: the in-app list of deleted meetings (HARMONY §7, §7.1; D100, D-1032).

Rows: the title, then `format.previewSubtitle` · days left, newest deletion first. Each row
has Restore (no confirmation) and Delete permanently… (confirmation 4) as buttons and in its
context menu; Delete on a focused row is Delete permanently…. The header's Empty Trash…
(confirmation 5) is insensitive while the Trash is empty. Return is Cancel on both
confirmations. Rows never open or play. File work runs on the trash worker
(meeting_trash.MeetingTrash); this view only renders its results.
"""

from datetime import UTC, datetime

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from . import ux_formats as fmt
from .inline_line import InlineLine
from .library_v2 import trash as storage
from .strings import plural, text


def _ms(value):
    if not value:
        return None
    try:
        return int(storage.parse_utc(value).timestamp() * 1000)
    except ValueError:
        return None


def row_meta(entry, *, now_ms, tz):
    """`format.previewSubtitle` · `trash.row.daysLeft` / `trash.row.lessThanDay`."""
    subtitle = fmt.preview_subtitle(
        _ms(entry.recorded_at_utc), entry.duration_ms, now_ms=now_ms, tz=tz, received=entry.received
    )
    now = datetime.fromtimestamp(now_ms / 1000, UTC)
    n, _expired = storage.days_left(entry.deleted_at_utc, now)
    left = text("trash.row.lessThanDay") if n < 1 else plural("trash.row.daysLeft", n)
    return f"{subtitle} · {left}"


def confirm(parent, heading, body, primary, on_confirm):
    """HARMONY §7 confirmations 4 and 5: Return and Esc are Cancel; the primary is destructive."""
    dialog = Adw.AlertDialog(heading=heading, body=body)
    dialog.add_response("cancel", text("common.cancel"))
    dialog.add_response("delete", primary)
    dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
    dialog.set_default_response("cancel")
    dialog.set_close_response("cancel")
    dialog.connect("response", lambda _d, response: on_confirm() if response == "delete" else None)

    def focus_cancel(*_args):
        # Return must reach Cancel even when focus was outside the alert (a header button).
        default = dialog.get_default_widget()
        if default is not None and dialog.get_mapped():
            default.grab_focus()

    dialog.connect("map", lambda *_: GLib.idle_add(focus_cancel))
    dialog.present(parent)
    return dialog


class TrashDialog(Adw.Dialog):
    def __init__(self, window):
        super().__init__(title=text("trash.title"), content_width=560, content_height=600)
        self.window = window
        self.controller = window.get_application().meeting_trash
        self.entries = None
        self.rows = {}
        self.confirmation = None
        self.closed = False
        self.problem = None

        self.empty_button = Gtk.Button(label=text("trash.emptyTrash"), sensitive=False)
        self.empty_button.connect("clicked", lambda *_: self.ask_empty())
        header = Adw.HeaderBar()
        header.pack_end(self.empty_button)

        self.list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"])
        self.list.update_property([Gtk.AccessibleProperty.LABEL], [text("trash.list.a11y")])
        self.lines = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.note = InlineLine("trash.retentionNote", info=True)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        content.set_margin_top(12)
        content.set_margin_bottom(18)
        content.set_margin_start(18)
        content.set_margin_end(18)
        content.append(self.lines)
        content.append(self.note)
        content.append(self.list)
        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        scroller.set_child(Adw.Clamp(child=content, maximum_size=640))

        self.status = Adw.StatusPage(icon_name="user-trash-symbolic", title=text("trash.empty"), vexpand=True)
        self.empty_lines = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        empty = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        empty.append(self.empty_lines)
        empty.append(self.status)
        self.stack = Gtk.Stack()
        self.stack.add_named(Adw.Spinner() if hasattr(Adw, "Spinner") else Gtk.Spinner(spinning=True), "loading")
        self.stack.add_named(empty, "empty")
        self.stack.add_named(scroller, "list")
        self.stack.set_visible_child_name("loading")

        view = Adw.ToolbarView(content=self.stack)
        view.add_top_bar(header)
        self.toasts = Adw.ToastOverlay(child=view)
        self.set_child(self.toasts)

        self.controller.listeners.append(self.reload)
        self.connect("closed", self._closed)
        self.reload()

    def _closed(self, *_args):
        self.closed = True
        if self.reload in self.controller.listeners:
            self.controller.listeners.remove(self.reload)

    # ---- data --------------------------------------------------------------------------

    def reload(self):
        if self.closed:
            return
        self.controller.entries(self._loaded)

    def _loaded(self, entries, error):
        if self.closed:
            return
        if error is not None:
            entries = []
        self.entries = list(entries)
        self._render()

    def _render(self):
        focused = self._focused_entry()
        while child := self.list.get_first_child():
            self.list.remove(child)
        self.rows = {}
        for entry in self.entries:
            row = self._row(entry)
            self.rows[entry.entry_id] = row
            self.list.append(row)
        self.empty_button.set_sensitive(bool(self.entries))
        self._show_problem()
        self.stack.set_visible_child_name("list" if self.entries else "empty")
        if focused in self.rows:
            self.rows[focused].grab_focus()

    def _focused_entry(self):
        focus = self.get_focus() if hasattr(self, "get_focus") else None
        while focus is not None and not hasattr(focus, "entry"):
            focus = focus.get_parent()
        return focus.entry.entry_id if focus is not None else None

    # ---- rows --------------------------------------------------------------------------

    def _row(self, entry):
        title = entry.title or text("library.untitled")
        row = Adw.ActionRow(
            title=GLib.markup_escape_text(title),
            subtitle=GLib.markup_escape_text(row_meta(entry, now_ms=self.window.now_ms, tz=self.window.tz)),
            activatable=False,
            focusable=True,
        )
        row.entry = entry
        restore = Gtk.Button(label=text("trash.restore"), valign=Gtk.Align.CENTER)
        restore.connect("clicked", lambda *_: self.restore(entry))
        delete = Gtk.Button(label=text("trash.deletePermanently"), valign=Gtk.Align.CENTER)
        delete.connect("clicked", lambda *_: self.ask_delete(entry))
        box = Gtk.Box(spacing=6)
        box.append(restore)
        box.append(delete)
        row.add_suffix(box)
        row.restore_button, row.delete_button = restore, delete

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._row_key, entry)
        row.add_controller(keys)

        group = Gio.SimpleActionGroup()
        for name, callback in (("restore", self.restore), ("delete", self.ask_delete)):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda *_a, cb=callback: cb(entry))
            group.add_action(action)
        row.insert_action_group("trash-row", group)
        row.context_menu = None
        click = Gtk.GestureClick(button=Gdk.BUTTON_SECONDARY)
        click.connect("pressed", lambda _g, _n, x, y: self.context_menu(row, x, y))
        row.add_controller(click)
        press = Gtk.GestureLongPress()
        press.connect("pressed", lambda _g, x, y: self.context_menu(row, x, y))
        row.add_controller(press)
        menu_key = Gtk.ShortcutController()
        menu_key.add_shortcut(
            Gtk.Shortcut(
                trigger=Gtk.ShortcutTrigger.parse_string("Menu|<Shift>F10"),
                action=Gtk.CallbackAction.new(lambda *_: self.context_menu(row, 24, 12) or True),
            )
        )
        row.add_controller(menu_key)
        return row

    def context_menu(self, row, x, y):
        """Restore and Delete permanently…, the same as the row's buttons (HARMONY §7.1)."""
        menu = Gio.Menu()
        menu.append(text("trash.restore"), "trash-row.restore")
        menu.append(text("trash.deletePermanently"), "trash-row.delete")
        popover = Gtk.PopoverMenu(menu_model=menu, has_arrow=False)
        popover.set_parent(row)
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
        popover.set_pointing_to(rect)
        # Unparent once closed (after its action ran), so a re-rendered row leaves nothing behind.
        popover.connect("closed", lambda p: GLib.idle_add(lambda: p.get_parent() and p.unparent()))
        row.context_menu = popover
        popover.popup()
        return popover

    def _row_key(self, _controller, key, _code, modifiers, entry):
        if key in (Gdk.KEY_Delete, Gdk.KEY_KP_Delete) and not modifiers & Gtk.accelerator_get_default_mod_mask():
            self.ask_delete(entry)
            return True
        return False

    # ---- problems ----------------------------------------------------------------------

    def _show_problem(self):
        for holder in (self.lines, self.empty_lines):
            while child := holder.get_first_child():
                holder.remove(child)
        if self.problem:
            holder = self.lines if self.entries else self.empty_lines
            holder.append(InlineLine(self.problem))

    def _report(self, problem):
        self.problem = problem
        self._show_problem()

    # ---- actions -----------------------------------------------------------------------

    def restore(self, entry):
        self._report(None)

        def done(_name, error):
            if self.closed:
                return
            if error is not None:
                self._report("trash.restoreFailed")
                return
            title = entry.title or text("library.untitled")
            self.toasts.add_toast(
                Adw.Toast(title=GLib.markup_escape_text(text("trash.restoredToast", title=title)), timeout=5)
            )

        self.controller.restore(entry.entry_id, done)

    def ask_delete(self, entry):
        title = entry.title or text("library.untitled")
        self.confirmation = confirm(
            self,
            text("confirm.deletePermanently.title", title=title),
            text("confirm.deletePermanently.body"),
            text("confirm.deletePermanently.primary"),
            lambda: self.delete(entry),
        )
        return self.confirmation

    def delete(self, entry):
        self._report(None)
        self.controller.purge(entry.entry_id, self._purged)

    def ask_empty(self):
        if not self.entries:
            return None
        self.confirmation = confirm(
            self,
            text("confirm.emptyTrash.title"),
            text("confirm.emptyTrash.body"),
            text("confirm.emptyTrash.primary"),
            self.empty,
        )
        return self.confirmation

    def empty(self):
        self._report(None)
        self.controller.empty(self._purged)

    def _purged(self, _result, error):
        if not self.closed and error is not None:
            self._report("trash.deleteFailed")
