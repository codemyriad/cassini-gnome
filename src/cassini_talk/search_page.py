# SPDX-License-Identifier: GPL-3.0-or-later
"""Library-wide phrase search. SQLite and transcript preparation run on one worker."""

import logging
from concurrent.futures import ThreadPoolExecutor
from threading import Event

from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk, Pango

from . import ux_formats as fmt
from .folder_player import FolderPlayer
from .search_index import SearchIndex
from .strings import text

LOG = logging.getLogger(__name__)


def is_playable(meeting):
    return meeting.has_audio and (meeting.audio_file is not None or FolderPlayer.can_play(meeting))


def box(spacing=0, **kwargs):
    return Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=spacing, **kwargs)


def label(value, *classes, **kwargs):
    return Gtk.Label(label=value, css_classes=list(classes), **kwargs)


class HitItem(GObject.Object):
    def __init__(self, hit, meeting):
        super().__init__()
        self.hit, self.meeting = hit, meeting


class HitModel(GObject.Object, Gio.ListModel):
    """Create result objects only for the visible portion of the list."""

    def __init__(self):
        super().__init__()
        self.hits = ()
        self.meetings = {}
        self._items = {}

    def do_get_item_type(self):
        return HitItem

    def do_get_n_items(self):
        return len(self.hits)

    def do_get_item(self, position):
        if position >= len(self.hits):
            return None
        reference = self._items.get(position)
        result = reference() if reference else None
        if result is None:
            hit = self.hits[position]
            result = HitItem(hit, self.meetings[hit.key])

            # GTK can retain the C object after its Python wrapper disappears.
            # A GObject weak reference preserves identity across that boundary.
            def finalized():
                current = self._items.get(position)
                if current is not None and current() is None:
                    self._items.pop(position, None)

            self._items[position] = result.weak_ref(finalized)
        return result

    def replace(self, hits, meetings):
        old = len(self.hits)
        self.hits, self.meetings = hits, meetings
        self._items.clear()
        self.items_changed(0, old, len(hits))


class SearchPage(Adw.Bin):
    __gsignals__ = {"results-updated": (GObject.SignalFlags.RUN_LAST, None, ())}

    def __init__(self, window):
        super().__init__(vexpand=True, hexpand=True)
        self.window = window
        self.speaker = None
        self.date = "any"
        self.closed = False
        self._cancelled = Event()
        self.pending = False
        self._generation = 0
        self._future = None
        self._timer = 0
        self._index = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cassini-search")
        self._snapshot = window.snapshot
        self._snapshot_signature = None
        self._meetings = {}
        self._turns = {}
        self.hit = None
        self._context_signature = None
        self._updating_selection = False
        self.hits = ()
        self.names = ()
        self._style_manager = Adw.StyleManager.get_default()
        self._style_handler = self._style_manager.connect("notify::dark", lambda *_: self.request())
        self.entry = window.search_entry
        self._had_nonempty_query = bool(self.entry.get_text())
        self._empty_close_source = 0
        self._entry_changed_handler = self.entry.connect("search-changed", self._on_entry_changed)
        self._entry_stop_handler = self.entry.connect("stop-search", lambda *_: window.back_to_library())

        left = box(6, vexpand=True, hexpand=True)
        filters = box(8, css_classes=["search-sidebar-filters"])
        chips = Gtk.FlowBox(
            selection_mode=Gtk.SelectionMode.NONE,
            column_spacing=6,
            row_spacing=6,
            homogeneous=False,
            max_children_per_line=4,
            css_classes=["search-filters"],
        )
        chips.update_property([Gtk.AccessibleProperty.LABEL], [text("library.filters.a11y")])
        # L-35 order: Starred · Speaker · Any date · Recorded by me. Starred
        # carries over the Library's at-rest choice; every chip resets on close.
        self._carried_starred = getattr(window, "library_filter", "all") == "starred"
        self.starred = Gtk.ToggleButton(
            label=text("search.filter.starred"), active=self._carried_starred, css_classes=["search-chip"]
        )
        self.starred.connect("toggled", lambda *_: self.request())
        self.speaker_button = Gtk.MenuButton(label=text("search.filter.speakerNone"), css_classes=["search-chip"])
        self.date_button = Gtk.MenuButton(label=text("search.filter.anyDate"), css_classes=["search-chip"])
        self.own = Gtk.ToggleButton(label=text("search.filter.recordedByMe"), css_classes=["search-chip"])
        self.own.connect("toggled", lambda *_: self.request())
        for widget in (self.starred, self.speaker_button, self.date_button, self.own):
            widget.set_halign(Gtk.Align.START)
            chips.append(widget)
            parent = widget.get_parent()
            if isinstance(parent, Gtk.FlowBoxChild):
                parent.set_can_focus(False)
        filters.append(chips)
        self.count = label(fmt.search_count(0, 0), "search-count-heading", xalign=0)
        filters.append(self.count)
        left.append(filters)
        self.store = HitModel()
        self.selection = Gtk.SingleSelection.new(self.store)
        self._selection_handler = self.selection.connect("notify::selected-item", self._selected)
        factory = Gtk.SignalListItemFactory()
        factory.connect("bind", self._bind)
        factory.connect("unbind", self._unbind)
        self.list = Gtk.ListView(model=self.selection, factory=factory, css_classes=["search-results"])
        self.list.connect("activate", lambda *_: self.open_meeting())
        row_click = Gtk.GestureClick(button=1)
        row_click.connect("released", self._on_result_row_released)
        self.list.add_controller(row_click)
        list_shortcuts = Gtk.ShortcutController(scope=Gtk.ShortcutScope.LOCAL)
        list_shortcuts.add_shortcut(
            Gtk.Shortcut.new(
                Gtk.KeyvalTrigger.new(Gdk.KEY_Right, 0),
                Gtk.CallbackAction.new(self._on_context_shortcut),
            )
        )
        self.list.add_controller(list_shortcuts)
        self.list.update_property([Gtk.AccessibleProperty.LABEL], [text("search.results.a11y")])
        self.scroller = Gtk.ScrolledWindow(child=self.list, hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        self._vadj = self.scroller.get_vadjustment()
        self._scroll_handler = self._vadj.connect("value-changed", lambda *_: window._queue_sidebar_scrolled())
        left.append(self.scroller)
        self.set_child(left)

        self.content_pane = Adw.ToolbarView(css_classes=["meeting-pane"])
        self.content_header = Adw.HeaderBar(
            show_start_title_buttons=False,
            show_back_button=False,
            show_end_title_buttons=True,
            decoration_layout=":close",
            show_title=False,
            css_classes=["meeting-topbar"],
        )
        self.back_button = Gtk.Button(
            icon_name="go-previous-symbolic",
            tooltip_text=text("search.results.a11y"),
            css_classes=["flat", "meeting-icon-btn"],
            visible=False,
        )
        self.back_button.update_property([Gtk.AccessibleProperty.LABEL], [text("search.results.a11y")])
        self.back_button.connect("clicked", lambda *_: self.show_results())
        self.content_header.pack_start(self.back_button)
        self._split_collapsed_handler = 0
        if hasattr(window, "split") and window.split is not None:
            self._split_collapsed_handler = window.split.connect("notify::collapsed", self._sync_back_button)
        self._sync_back_button()
        self.content_pane.add_top_bar(self.content_header)
        self.context = box(24, margin_start=48, margin_end=48, margin_top=24, margin_bottom=30)
        self.context.update_property([Gtk.AccessibleProperty.LABEL], [text("search.context.a11y")])
        self.context_scroller = Gtk.ScrolledWindow(
            child=self.context, hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True
        )
        self._context_scroll_source = 0
        self._context_vadj = self.context_scroller.get_vadjustment()
        self._context_scroll_handler = self._context_vadj.connect("value-changed", self._queue_context_scrolled)
        self.content_pane.set_content(self.context_scroller)
        self._speaker_menu()
        self._date_menu()
        self.request()

    def _is_split_collapsed(self):
        return bool(
            hasattr(self.window, "split") and self.window.split is not None and self.window.split.get_collapsed()
        )

    def _sync_back_button(self, *_):
        self.back_button.set_visible(self._is_split_collapsed())

    def show_context(self):
        if not self.hit:
            return
        if hasattr(self.window, "split") and self.window.split is not None:
            self.window.split.set_show_content(True)
            if self.window.split.get_collapsed():
                target = getattr(self, "play", None)
                if target is None or not target.get_sensitive():
                    target = getattr(self, "open", None)
                if target is not None:
                    target.grab_focus()

    def show_results(self):
        if hasattr(self.window, "split") and self.window.split is not None:
            self.window.split.set_show_content(False)
        self.list.grab_focus()

    def _on_result_row_released(self, _gesture, n_press, x, y):
        if n_press == 1 and self._is_split_collapsed() and self.hit is not None:
            picked = self.list.pick(x, y, Gtk.PickFlags.DEFAULT)
            if picked is not None and picked is not self.list:
                self.show_context()

    def _on_context_shortcut(self, *_):
        if self._is_split_collapsed() and self.hit is not None:
            self.show_context()
            return True
        return False

    def _queue_context_scrolled(self, *_args):
        if not self._context_scroll_source and not self.closed:
            self._context_scroll_source = GLib.idle_add(self._apply_context_scrolled)

    def _apply_context_scrolled(self):
        self._context_scroll_source = 0
        if not self.closed:
            scrolled = self._context_vadj.get_value() > 0.5
            (self.content_header.add_css_class if scrolled else self.content_header.remove_css_class)("scrolled")
        return GLib.SOURCE_REMOVE

    def _check_empty_close(self):
        self._empty_close_source = 0
        if (
            not self.closed
            and not getattr(self.window, "_closing_search", False)
            and not self.entry.get_text()
            and self._had_nonempty_query
            and not self._filters_changed()
        ):
            self._had_nonempty_query = False
            self.window.back_to_library()
        return GLib.SOURCE_REMOVE

    def _on_entry_changed(self, *_args):
        if self.closed or getattr(self.window, "_closing_search", False):
            return
        value = self.entry.get_text()
        if value:
            if self._empty_close_source:
                GLib.source_remove(self._empty_close_source)
                self._empty_close_source = 0
            self._had_nonempty_query = True
        elif self._had_nonempty_query and not self._filters_changed():
            # GtkEditable.set_text("new") deletes to "" before inserting "new" in the
            # same C call; check on idle so replacing text never closes Search mid-call.
            if not self._empty_close_source:
                self._empty_close_source = GLib.idle_add(self._check_empty_close)
            return
        self.request()

    def _filters_changed(self):
        """Whether the user narrowed Search beyond what it opened with."""
        return bool(
            self.speaker is not None
            or self.date != "any"
            or self.own.get_active()
            or self.starred.get_active() != self._carried_starred
        )

    def stars_changed(self):
        """Stars changed in this window: refresh the filter and the row glyphs."""
        self.request()

    def _menu(self, button, choices, selected, callback):
        popover = Gtk.Popover()
        items = box(2, margin_start=6, margin_end=6, margin_top=6, margin_bottom=6)
        group = None
        for value, name in choices:
            row = Gtk.CheckButton(label=name)
            if group is not None:
                row.set_group(group)
            else:
                group = row
            row.set_active(value == selected)
            # One choice, closing the menu immediately; no persisted toggles.
            row.connect(
                "toggled", lambda check, item=value: (callback(item), popover.popdown()) if check.get_active() else None
            )
            items.append(row)
        scroller = Gtk.ScrolledWindow(
            child=items, hscrollbar_policy=Gtk.PolicyType.NEVER, propagate_natural_height=True, max_content_height=360
        )
        popover.set_child(scroller)
        button.set_popover(popover)

    def _speaker_menu(self):
        self._menu(
            self.speaker_button,
            [(None, text("search.filter.anySpeaker"))] + [(n, n) for n in self.names],
            self.speaker,
            self.choose_speaker,
        )

    def choose_speaker(self, name):
        self.speaker = name
        self.speaker_button.set_label(
            text("search.filter.speaker", name=name) if name else text("search.filter.speakerNone")
        )
        (self.speaker_button.add_css_class if name else self.speaker_button.remove_css_class)("suggested-action")
        self._speaker_menu()
        self.request()

    def _date_menu(self):
        choices = [("any", text("search.filter.anyDate"))] + [
            (key, text("search.filter.date." + key)) for key in ("today", "week", "month", "year")
        ]
        self._menu(self.date_button, choices, self.date, self.choose_date)

    def choose_date(self, choice):
        self.date = choice
        self.date_button.set_label(
            text("search.filter.anyDate") if choice == "any" else text("search.filter.date." + choice)
        )
        (self.date_button.add_css_class if choice != "any" else self.date_button.remove_css_class)("suggested-action")
        self._date_menu()
        self.request()

    def update(self, snapshot):
        self._snapshot = snapshot
        signature = (
            snapshot.unavailable,
            self.window.get_application().voice_store.match_signature(),
            tuple(
                (
                    str(m.directory),
                    m.key,
                    id(m.words),
                    m.speakers,
                    m.title,
                    m.display_start,
                    m.sort_time,
                    m.duration_ms,
                    m.origin,
                    m.has_audio,
                    is_playable(m),
                )
                for m in snapshot.meetings
            ),
        )
        if signature != self._snapshot_signature:
            self._snapshot_signature = signature
            self.request()

    def _starred_keys(self):
        stars = getattr(self.window, "_stars", None)
        return frozenset(stars.starred) if stars is not None else frozenset()

    def request(self):
        if self.closed:
            return
        self._generation += 1
        self.pending = True
        if self._timer:
            GLib.source_remove(self._timer)
        self._timer = GLib.timeout_add(100, self._dispatch)

    def _dispatch(self):
        self._timer = 0
        if self.closed or self._future:
            return GLib.SOURCE_REMOVE
        generation = self._generation
        snapshot = self._snapshot
        query = self.entry.get_text()
        filters = dict(
            speaker=self.speaker,
            date=self.date,
            own=self.own.get_active(),
            starred=self._starred_keys() if self.starred.get_active() else None,
            now_ms=self.window.now_ms,
            tz=self.window.tz,
            dark=self._style_manager.get_dark(),
        )
        root = self.window.monitor.scanner.root

        def work():
            if snapshot is None:
                return (), (), {}, {}
            if self._index is not None and getattr(self, "_index_root", None) != root:
                self._index.close()
                self._index = None
            if self._index is None:
                self._index = SearchIndex(root, cancelled=self._cancelled)
                self._index_root = root
            self._index.sync(snapshot)
            return (
                self._index.query(query, **filters),
                self._index.speakers(),
                dict(self._index.current),
                dict(self._index.turns),
            )

        self._future = self._executor.submit(work)
        self._future.add_done_callback(lambda future: GLib.idle_add(self._complete, future, generation))
        return GLib.SOURCE_REMOVE

    def _complete(self, future, generation):
        self._future = None
        if self.closed:
            return GLib.SOURCE_REMOVE
        if generation != self._generation:
            # Dispatch the newest query now, consuming its debounce. Leaving
            # that timer armed can dispatch the same generation a second time.
            if self._timer:
                GLib.source_remove(self._timer)
                self._timer = 0
            self._dispatch()
            return GLib.SOURCE_REMOVE
        try:
            hits, names, self._meetings, self._turns = future.result()
        except Exception:
            LOG.exception("Search index update failed")
            hits, names = (), ()
        if names != self.names:
            self.names = names
            self._speaker_menu()
        old = (self.hit.key, self.hit.turn) if self.hit else None
        self.hits = hits
        self.count.set_label(fmt.search_count(len(hits), len({h.key for h in hits})))
        self._updating_selection = True
        try:
            self.selection.handler_block(self._selection_handler)
            self.store.replace(hits, self._meetings)
            selected = next((i for i, h in enumerate(hits) if (h.key, h.turn) == old), 0)
            self.selection.set_selected(selected if hits else Gtk.INVALID_LIST_POSITION)
            self.selection.handler_unblock(self._selection_handler)
            self._selected()
        finally:
            self._updating_selection = False
        if not hits and self._is_split_collapsed() and self.window.split.get_show_content():
            self.window.split.set_show_content(False)
        self.pending = False
        self.emit("results-updated")
        return GLib.SOURCE_REMOVE

    def _bind(self, _factory, item):
        result = item.get_item()
        hit, meeting = result.hit, result.meeting
        row = box(4, css_classes=["search-result"])
        heading = Gtk.Box(spacing=8)
        if self.window.is_starred(meeting):
            from .library_window import star_glyph

            heading.append(star_glyph())
            row.update_property([Gtk.AccessibleProperty.DESCRIPTION], [text("library.row.starred.a11y")])
        heading.append(
            label(
                hit.title_markup or meeting.title,
                "row-title",
                use_markup=bool(hit.title_markup),
                xalign=0,
                hexpand=True,
                wrap=True,
            )
        )
        heading.append(
            label(
                fmt.search_date(meeting.display_start, now_ms=self.window.now_ms, tz=self.window.tz),
                "small",
                "dim-label",
                "row-num",
                valign=Gtk.Align.START,
            )
        )
        row.append(heading)
        quote = label(
            text("common.quote", quote=(hit.selected_quote_markup if item.get_selected() else hit.quote_markup)),
            "search-quote",
            use_markup=True,
            xalign=0,
            wrap=True,
            lines=2,
            ellipsize=Pango.EllipsizeMode.END,
        )
        quote.set_visible(bool(hit.quote_markup))
        row.append(quote)
        speaker = self._speaker(hit)
        row.append(
            label(
                fmt.position(hit.position_ms)
                if hit.turn < 0
                else text(
                    "search.result.meta", name=speaker.name if speaker else "", position=fmt.position(hit.position_ms)
                ),
                "small",
                "dim-label",
                "row-num",
                xalign=0,
                ellipsize=Pango.EllipsizeMode.END,
            )
        )
        item.set_child(row)
        item._selected_handler = item.connect(
            "notify::selected",
            lambda selected, *_: quote.set_label(
                text("common.quote", quote=(hit.selected_quote_markup if selected.get_selected() else hit.quote_markup))
            ),
        )

    def _unbind(self, _factory, item):
        if getattr(item, "_selected_handler", None):
            item.disconnect(item._selected_handler)
            item._selected_handler = None
        item.set_child(None)

    def _speaker(self, hit):
        if hit.turn < 0:
            return None
        return self._meetings[hit.key].speaker_for(hit.speaker or self._turns[hit.key][hit.turn].speaker)

    def _selected(self, *_):
        chosen = self.selection.get_selected_item()
        hit = chosen.hit if chosen else None
        if self.hit and (
            not hit or (hit.key, hit.turn, hit.position_ms) != (self.hit.key, self.hit.turn, self.hit.position_ms)
        ):
            self.window._stop_playback()
        self.hit = hit
        meeting = self._meetings[hit.key] if hit else None
        playable = is_playable(meeting) if meeting else False
        context_text = (
            fmt.search_context(
                meeting.display_start,
                meeting.duration_ms,
                hit.position_ms,
                now_ms=self.window.now_ms,
                tz=self.window.tz,
            )
            if hit
            else None
        )
        signature = (
            (
                hit,
                meeting.title,
                meeting.has_audio,
                playable,
                meeting.speakers,
                self._turns[hit.key][max(0, hit.turn - 1) : hit.turn + 2],
                self.window.get_application().voice_store.match_signature(),
                context_text,
            )
            if hit
            else None
        )
        # Preserve focused/AT-SPI controls when a refresh leaves the displayed
        # result unchanged. Removing an activating button can cancel its click.
        if signature == self._context_signature:
            if (
                not getattr(self, "_updating_selection", False)
                and hit is not None
                and SearchPage._is_split_collapsed(self)
            ):
                SearchPage.show_context(self)
            return
        self._context_signature = signature
        child = self.context.get_first_child()
        while child:
            following = child.get_next_sibling()
            self.context.remove(child)
            child = following
        if not hit:
            return
        meeting = self._meetings[hit.key]
        heading = box(10)
        heading.append(
            label(
                hit.title_markup or meeting.title,
                "library-title",
                use_markup=bool(hit.title_markup),
                xalign=0,
                wrap=True,
            )
        )
        heading.append(
            label(
                context_text,
                "dim-label",
                xalign=0,
                wrap=True,
            )
        )
        self.context.append(heading)
        turns = self._turns[hit.key]
        excerpt = box(22, margin_top=8)
        for index in range(max(0, hit.turn - 1), min(len(turns), hit.turn + 2)):
            turn = turns[index]
            speaker = meeting.speaker_for(turn.speaker)
            row = Gtk.Box(spacing=16, css_classes=[] if index == hit.turn else ["dim-label"])
            name = box(6, width_request=110, valign=Gtk.Align.START)
            if speaker:
                name.append(self.window._speaker(speaker, meeting=meeting))
            if index == hit.turn:
                name.append(label(fmt.position(hit.position_ms), "small", "dim-label", xalign=0))
            row.append(name)
            row.append(
                label(
                    hit.context_markup
                    if index == hit.turn
                    else GLib.markup_escape_text(turn.text[:800] + ("…" if len(turn.text) > 800 else "")),
                    "search-quote",
                    use_markup=True,
                    wrap=True,
                    xalign=0,
                    hexpand=True,
                    valign=Gtk.Align.START,
                )
            )
            excerpt.append(row)
        self.context.append(excerpt)
        actions = Gtk.FlowBox(
            selection_mode=Gtk.SelectionMode.NONE,
            column_spacing=10,
            row_spacing=10,
            homogeneous=False,
            max_children_per_line=2,
            css_classes=["search-actions"],
        )
        self.play = Gtk.Button(css_classes=["suggested-action"], sensitive=playable)
        self.play.set_child(
            Adw.ButtonContent(
                icon_name="media-playback-start-symbolic",
                label=text("search.context.playFrom", position=fmt.position(hit.position_ms)),
            )
        )
        self.play.connect("clicked", lambda *_: self.play_from())
        self.open = Gtk.Button(label=text("search.context.openMeeting"))
        self.open.connect("clicked", lambda *_: self.open_meeting())
        actions.append(self.play)
        actions.append(self.open)
        self.context.append(actions)
        if not getattr(self, "_updating_selection", False) and SearchPage._is_split_collapsed(self):
            SearchPage.show_context(self)

    def play_from(self):
        if self.hit:
            meeting = self._meetings[self.hit.key]
            if self.window._ensure_player(meeting):
                self.window.player.seek(self.hit.position_ms)
                self.window.player.play()

    def open_meeting(self):
        if self.hit:
            meeting, position = self._meetings[self.hit.key], self.hit.position_ms
            self.window.open_meeting(meeting)
            self.window.player.seek(position)

    def close(self):
        self.closed = True
        self._cancelled.set()
        if self._entry_changed_handler:
            self.entry.disconnect(self._entry_changed_handler)
            self._entry_changed_handler = 0
        if self._entry_stop_handler:
            self.entry.disconnect(self._entry_stop_handler)
            self._entry_stop_handler = 0
        if self._scroll_handler:
            self._vadj.disconnect(self._scroll_handler)
            self._scroll_handler = 0
        if self._context_scroll_handler:
            self._context_vadj.disconnect(self._context_scroll_handler)
            self._context_scroll_handler = 0
        if self._context_scroll_source:
            GLib.source_remove(self._context_scroll_source)
            self._context_scroll_source = 0
        if self._empty_close_source:
            GLib.source_remove(self._empty_close_source)
            self._empty_close_source = 0
        if self._style_handler:
            self._style_manager.disconnect(self._style_handler)
            self._style_handler = 0
        if self._split_collapsed_handler and hasattr(self.window, "split") and self.window.split is not None:
            self.window.split.disconnect(self._split_collapsed_handler)
            self._split_collapsed_handler = 0
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = 0
        self._executor.submit(lambda: self._index.close() if self._index else None)
        self._executor.shutdown(wait=False)
