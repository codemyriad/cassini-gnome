# SPDX-License-Identifier: GPL-3.0-or-later
"""Library-wide phrase search. SQLite and transcript preparation run on one worker."""

import logging
from concurrent.futures import ThreadPoolExecutor
from threading import Event

from gi.repository import Adw, Gio, GLib, GObject, Gtk

from . import ux_formats as fmt
from .search_index import SearchIndex
from .strings import text

LOG = logging.getLogger(__name__)


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


class SearchPage(Adw.NavigationPage):
    def __init__(self, window):
        super().__init__(title=text("search.title"))
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
        self.hits = ()
        self.names = ()
        split = Adw.NavigationSplitView(min_sidebar_width=460, max_sidebar_width=460, sidebar_width_fraction=0.41)
        split.set_sidebar_width_unit(Adw.LengthUnit.PX)
        self.set_child(split)
        sidebar = Adw.ToolbarView(css_classes=["library-sidebar"])
        header = Adw.HeaderBar(
            show_start_title_buttons=False,
            show_end_title_buttons=False,
            show_back_button=False,
            css_classes=["library-header"],
        )
        header.set_title_widget(Gtk.Box())
        self.capture = window.capture_controls()
        header.pack_start(self.capture)
        toggle = Gtk.ToggleButton(
            icon_name="system-search-symbolic",
            active=True,
            action_name="win.search",
            tooltip_text=text("common.search"),
        )
        header.pack_end(toggle)
        sidebar.add_top_bar(header)
        left = box(12)
        filters = box(12, margin_start=20, margin_end=20, margin_top=14)
        self.entry = Gtk.SearchEntry(placeholder_text=text("search.field.placeholder"), height_request=36)
        self.entry.update_property([Gtk.AccessibleProperty.LABEL], [text("search.field.a11y")])
        window.protect_text_input(self.entry)
        self.entry.connect("search-changed", lambda *_: self.request())
        self.entry.connect("stop-search", lambda *_: window.back_to_library())
        filters.append(self.entry)
        chips = Gtk.Grid(row_spacing=8, column_spacing=8)
        self.speaker_button = Gtk.MenuButton(label=text("search.filter.speakerNone"), css_classes=["search-chip"])
        self.date_button = Gtk.MenuButton(label=text("search.filter.anyDate"), css_classes=["search-chip"])
        self.own = Gtk.ToggleButton(label=text("search.filter.recordedByMe"), css_classes=["search-chip"])
        self.own.connect("toggled", lambda *_: self.request())
        for i, widget in enumerate((self.speaker_button, self.date_button, self.own)):
            chips.attach(widget, i % 2, i // 2, 1, 1)
            widget.set_halign(Gtk.Align.START)
        filters.append(chips)
        self.count = label(fmt.search_count(0, 0), "small", "dim-label", xalign=0)
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
        self.list.update_property([Gtk.AccessibleProperty.LABEL], [text("search.results.a11y")])
        left.append(Gtk.ScrolledWindow(child=self.list, hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True))
        sidebar.set_content(left)
        split.set_sidebar(Adw.NavigationPage.new(sidebar, text("search.results.a11y")))
        content = Adw.ToolbarView()
        header = Adw.HeaderBar(
            show_start_title_buttons=False,
            show_back_button=False,
            decoration_layout=":close",
            css_classes=["library-header"],
        )
        header.set_title_widget(label(text("search.title"), "heading"))
        content.add_top_bar(header)
        self.context = box(24, margin_start=36, margin_end=36, margin_top=34, margin_bottom=30)
        self.context.update_property([Gtk.AccessibleProperty.LABEL], [text("search.context.a11y")])
        content.set_content(Gtk.ScrolledWindow(child=self.context, hscrollbar_policy=Gtk.PolicyType.NEVER))
        split.set_content(Adw.NavigationPage.new(content, text("search.context.a11y")))
        self._speaker_menu()
        self._date_menu()
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
            (key, text("search.filter.date." + key)) for key in ("today", "week", "month")
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
                    m.started_at_ms,
                    m.duration_ms,
                    m.origin,
                    m.has_audio,
                )
                for m in snapshot.meetings
            ),
        )
        if signature != self._snapshot_signature:
            self._snapshot_signature = signature
            self.request()

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
            now_ms=self.window.now_ms,
            tz=self.window.tz,
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
            self._dispatch()
            return GLib.SOURCE_REMOVE
        self.pending = False
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
        self.selection.handler_block(self._selection_handler)
        self.store.replace(hits, self._meetings)
        selected = next((i for i, h in enumerate(hits) if (h.key, h.turn) == old), 0)
        self.selection.set_selected(selected if hits else Gtk.INVALID_LIST_POSITION)
        self.selection.handler_unblock(self._selection_handler)
        self._selected()
        return GLib.SOURCE_REMOVE

    def _bind(self, _factory, item):
        result = item.get_item()
        hit, meeting = result.hit, result.meeting
        row = box(8, css_classes=["search-result"], margin_start=12, margin_end=12, margin_top=4, margin_bottom=4)
        heading = Gtk.Box(spacing=8)
        heading.append(label(meeting.title, "heading", xalign=0, hexpand=True, wrap=True))
        heading.append(
            label(
                fmt.search_date(meeting.started_at_ms, now_ms=self.window.now_ms, tz=self.window.tz),
                "small",
                "dim-label",
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
        )
        row.append(quote)
        speaker = self._speaker(hit)
        row.append(
            label(
                text(
                    "search.result.meta", name=speaker.name if speaker else "", position=fmt.position(hit.position_ms)
                ),
                "small",
                "dim-label",
                xalign=0,
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
        return next(
            (s for s in self._meetings[hit.key].speakers if s.id == self._turns[hit.key][hit.turn].speaker), None
        )

    def _selected(self, *_):
        chosen = self.selection.get_selected_item()
        hit = chosen.hit if chosen else None
        if self.hit and (
            not hit or (hit.key, hit.turn, hit.position_ms) != (self.hit.key, self.hit.turn, self.hit.position_ms)
        ):
            self.window._stop_playback()
        self.hit = hit
        child = self.context.get_first_child()
        while child:
            following = child.get_next_sibling()
            self.context.remove(child)
            child = following
        if not hit:
            return
        meeting = self._meetings[hit.key]
        heading = box(10)
        heading.append(label(meeting.title, "library-title", xalign=0, wrap=True))
        heading.append(
            label(
                fmt.search_context(
                    meeting.started_at_ms,
                    meeting.duration_ms,
                    hit.position_ms,
                    now_ms=self.window.now_ms,
                    tz=self.window.tz,
                ),
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
            speaker = next((s for s in meeting.speakers if s.id == turn.speaker), None)
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
        self.play = Gtk.Button(css_classes=["suggested-action"], sensitive=meeting.has_audio)
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
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = 0
        self.window._capture_headers.remove(self.capture._controls)
        self.window._focused_editors.discard(id(self.entry))
        self.window._editor_focus(self.entry, False)
        self._executor.submit(lambda: self._index.close() if self._index else None)
        self._executor.shutdown(wait=False)
