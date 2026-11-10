# SPDX-License-Identifier: GPL-3.0-or-later
"""Experimental whole-buffer transcript. Enable with CASSINI_TRANSCRIPT_RENDER=textview.

GTK owns scrolling and layout; no application kinetic controller or row estimates.
Offsets are Unicode character offsets, never UTF-8 bytes. The shared reading rows
remain the authority for ordering, repaired timings, stored text and folded chips.
"""

from bisect import bisect_right

from gi.repository import Gdk, Gtk, Pango

from . import ux_formats as fmt
from .meeting_text import row_caption


class TranscriptTextView(Gtk.TextView):
    def __init__(self, page):
        super().__init__(
            editable=False,
            cursor_visible=True,
            wrap_mode=Gtk.WrapMode.WORD_CHAR,
            left_margin=32,
            right_margin=32,
            top_margin=12,
            bottom_margin=24,
            css_classes=["transcript-textview"],
        )
        self.page = page
        self.buffer = self.get_buffer()
        self.ranges = []
        self.offsets = []
        self.rows = []
        self._model = None
        self._active = set()
        self._time_starts = []
        self._time_ends = []
        self._last_position = None
        self._start_cursor = self._end_cursor = 0
        tabs = Pango.TabArray.new(1, True)
        tabs.set_tab(0, Pango.TabAlign.LEFT, 220)
        self.body = self.buffer.create_tag(
            "body",
            left_margin=32,
            indent=-220,
            tabs=tabs,
            pixels_below_lines=24,
            line_height=1.5,
            font_desc=Pango.FontDescription.from_string("15px"),
        )
        self.plain = self.buffer.create_tag(
            "plain", pixels_below_lines=24, line_height=1.5, font_desc=Pango.FontDescription.from_string("15px")
        )
        self.heading = self.buffer.create_tag("heading", weight=Pango.Weight.BOLD)
        self.caption = self.buffer.create_tag("caption", style=Pango.Style.ITALIC)
        self.chip = self.buffer.create_tag("chip", background="#e9e9ed", scale=0.9)
        self.uncertain = self.buffer.create_tag("uncertain", style=Pango.Style.ITALIC)
        self.current = self.buffer.create_tag("current", background="#fbe7a1", foreground="#202024")
        # Observe the TextView's own click gesture so its selection gesture
        # cannot deny a competing gesture before the release reaches us.
        controllers = self.observe_controllers()
        for i in range(controllers.get_n_items()):
            click = controllers.get_item(i)
            if isinstance(click, Gtk.GestureClick):
                click.connect("released", self._released)
                break
        else:
            raise RuntimeError("GtkTextView has no native click gesture")
        key = Gtk.EventControllerKey()
        key.connect("key-pressed", self._key)
        self.add_controller(key)

    def _tag(self, tag, start, end):
        self.buffer.apply_tag(tag, self.buffer.get_iter_at_offset(start), self.buffer.get_iter_at_offset(end))

    def set_meeting(self, meeting):
        signature = (meeting.turns, meeting.speakers, bool(meeting.speakers))
        if signature == self._model:
            return
        self._model = signature
        self.ranges, self.rows = [], []
        pieces, tags, offset = [], [], 0

        def append(value, tag=None, word=None):
            nonlocal offset
            start = offset
            pieces.append(value)
            offset += len(value)
            if tag is not None:
                tags.append((tag, start, offset))
            if word is not None and value:
                self.ranges.append((start, offset, word))
                if word.low_confidence:
                    tags.append((self.uncertain, start, offset))

        def names(speaker):
            return meeting.speaker_for(speaker).name

        for row in meeting.turns:
            start = offset
            self.rows.append(start)
            if meeting.speakers:
                # Hanging paragraph: header in a tabbed left column, wrapped
                # body lines start at the body tab. Long names need ellipsis
                # or an anchored header widget before production adoption.
                append(
                    ("" if row.continuation else names(row.speaker)) + " · " + fmt.position(row.start_ms), self.heading
                )
                append("\t")
            caption = row_caption(row, names)
            if caption:
                append(caption + " · ", self.caption)
            asides = {}
            for after, aside in row.asides:
                asides.setdefault(after, []).append(aside)
            emitted = False
            for index, value in enumerate(row.items):
                if value:
                    if emitted:
                        append(" ")
                    append(value, word=row.words[index])
                    emitted = True
                for aside in asides.get(index, ()):
                    if emitted:
                        append(" ")
                    chip_start = offset
                    append("(" + names(aside.speaker) + ": ")
                    first = True
                    for word, item in zip(aside.words, aside.items, strict=True):
                        if item:
                            if not first:
                                append(" ")
                            append(item, word=word)
                            first = False
                    append(")")
                    tags.append((self.chip, chip_start, offset))
                    emitted = True
            append("\n")
            tags.append((self.body if meeting.speakers else self.plain, start, offset))
        self.buffer.set_text("".join(pieces))
        for tag, start, end in tags:
            self._tag(tag, start, end)
        self.offsets = [start for start, _end, _word in self.ranges]
        self._time_starts = sorted((word.start_ms, i) for i, (_a, _b, word) in enumerate(self.ranges))
        self._time_ends = sorted((word.end_ms, i) for i, (_a, _b, word) in enumerate(self.ranges))
        self._active = set()
        self._last_position = None
        self.highlight(self.page.player.position)

    def highlight(self, position):
        # Walk temporal boundaries during normal playback. A seek scans the
        # index once. Never inspect or rebuild the whole buffer each tick.
        active = set(self._active)
        if self._last_position is None or position < self._last_position:
            active = {i for i, (_a, _b, word) in enumerate(self.ranges) if word.start_ms <= position < word.end_ms}
            self._start_cursor = bisect_right(self._time_starts, (position, len(self.ranges)))
            self._end_cursor = bisect_right(self._time_ends, (position, len(self.ranges)))
        else:
            while self._start_cursor < len(self._time_starts) and self._time_starts[self._start_cursor][0] <= position:
                active.add(self._time_starts[self._start_cursor][1])
                self._start_cursor += 1
            while self._end_cursor < len(self._time_ends) and self._time_ends[self._end_cursor][0] <= position:
                active.discard(self._time_ends[self._end_cursor][1])
                self._end_cursor += 1
        for i in active ^ self._active:
            start, end, _word = self.ranges[i]
            if i in active:
                self._tag(self.current, start, end)
            else:
                self.buffer.remove_tag(
                    self.current, self.buffer.get_iter_at_offset(start), self.buffer.get_iter_at_offset(end)
                )
        self._active = active
        self._last_position = position

    def follow_current(self):
        """Keep the active word visible even within a long reading row."""
        if not self._active:
            return
        start = self.ranges[min(self._active)][0]
        iterator = self.buffer.get_iter_at_offset(start)
        location = self.get_iter_location(iterator)
        visible = self.get_visible_rect()
        if location.y < visible.y + 48 or location.y + location.height > visible.y + visible.height - 48:
            self.scroll_to_iter(iterator, 0.15, False, 0, 0)

    def scroll_to(self, index, *_args):
        if 0 <= index < len(self.rows):
            self.scroll_to_iter(self.buffer.get_iter_at_offset(self.rows[index]), 0.15, False, 0, 0)

    def word_at_offset(self, offset):
        i = bisect_right(self.offsets, offset) - 1
        if i >= 0 and self.ranges[i][0] <= offset < self.ranges[i][1]:
            return self.ranges[i][2]
        return None

    def _released(self, _gesture, count, x, y):
        if (
            count != 1
            or self.buffer.get_has_selection()
            or (_gesture is not None and _gesture.get_current_button() != Gdk.BUTTON_PRIMARY)
        ):
            return
        bx, by = self.window_to_buffer_coords(Gtk.TextWindowType.WIDGET, int(x), int(y))
        found, iterator = self.get_iter_at_location(bx, by)
        if found and (word := self.word_at_offset(iterator.get_offset())) is not None:
            self.page.player.seek(word.start_ms)

    def _key(self, _controller, key, _code, _state):
        if key in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            iterator = self.buffer.get_iter_at_mark(self.buffer.get_insert())
            if (word := self.word_at_offset(iterator.get_offset())) is not None:
                self.page.player.seek(word.start_ms)
                return True
        return False
