# SPDX-License-Identifier: GPL-3.0-or-later
"""Named native link actions for visible words, alongside GtkLabel Text.

GTK's virtual GtkLabel hyperlink children expose ranges but no Action. These
non-targetable, transparent buttons supply real AT-SPI actions without changing
pointer handling or adding duplicate keyboard stops. Only visible words exist.
"""

import re
from html import unescape
from time import thread_time_ns

from gi.repository import GLib, Graphene, Gsk, Gtk, Pango

from .i18n import language, using_language
from .strings import text

# This consumes only our escaped, canonical reading_markup output. It avoids
# importing/general-purpose HTML parsing on the first transcript opening.
# Canonical escaped markup has short entities. Split plain text into bounded
# tokens so even a long name yields between small parsing steps.
_TOKENS = re.compile(r"<[^>]*>|&[^;<&]{1,16};|[^<&]{1,256}|&")


class LinkSpans:
    def __init__(self, markup):
        self.spans = [span for span in self.scan(markup) if span is not None]

    @staticmethod
    def scan(markup):
        """Yield after each token, returning completed timed-word links."""
        offset, current, seen = 0, None, set()
        for match in _TOKENS.finditer(markup):
            span = None
            token = match[0]
            if token.startswith('<a href="'):
                current = [token[9:-2], offset, [], False]
            elif token == "<b>" and current:
                current[3] = True
            elif token == "</a>" and current:
                uri, start, parts, synthetic = current
                if not synthetic and not uri.endswith(":anchor") and uri not in seen:
                    value = "".join(parts)
                    if value.strip():
                        span = uri, start, value
                        seen.add(uri)
                current = None
            elif not token.startswith("<"):
                value = unescape(token)
                if current and not current[3]:
                    current[2].append(value)
                offset += len(value.encode())
            yield span


class WordActions:
    def __init__(self, view):
        self.view, self.buttons = view, {}
        self.generation, self._tick = 0, 0

    def _retry(self):
        if not self._tick:
            self._tick = self.view.add_tick_callback(self._frame)

    def _frame(self, *_):
        self._tick = 0
        self.view.queue_allocate()
        return GLib.SOURCE_REMOVE

    def clear(self):
        if self._tick:
            self.view.remove_tick_callback(self._tick)
            self._tick = 0
        for button in self.buttons.values():
            if button.get_parent():
                button.unparent()
        self.buttons.clear()

    def update(self, height):
        view, needed = self.view, set()
        if not view.items:
            return
        # Paint readable native Text first. Creating the supplemental native
        # Action objects is then bounded separately from text indexing.
        if not view.viewport_ready:
            self._retry()
            return
        deadline = thread_time_ns() + 2_000_000
        missing = False
        pending_rows = set()
        signature = getattr(view.page, "_header_signature", None)
        with using_language(signature[-1] if signature else language()):
            description = text("player.playFromHere")
        if getattr(self, "description", None) != description:
            self.description = description
            for button in self.buttons.values():
                button.update_property([Gtk.AccessibleProperty.DESCRIPTION], [description])
        zero = Graphene.Point().init(0, 0)
        for number, row in view.rows.items():
            if number not in view.visible_rows:
                continue
            top = view.prefix[number] - view._value()
            if top >= height or view.prefix[number + 1] - view._value() <= 0:
                continue
            words = row.get_last_child()
            plain = words.get_text()
            ok, origin = words.compute_point(row, zero)
            if not ok:
                continue
            lx, ly = words.get_layout_offsets()
            geometry_key = self.generation, plain, view.layout_width, origin.x, origin.y, lx, ly
            if getattr(row, "_action_geometry_key", None) != geometry_key:
                if thread_time_ns() >= deadline:
                    missing = True
                    pending_rows.add(number)
                    continue
                row._action_geometry_key = geometry_key
                row._action_geometry = []
                row._action_pending = LinkSpans.scan(words.get_label())
            layout = words.get_layout()
            while row._action_pending is not None and thread_time_ns() < deadline:
                try:
                    span = next(row._action_pending)
                except StopIteration:
                    row._action_pending = None
                    break
                if span is None:
                    continue
                uri, start, value = span
                rect = layout.index_to_pos(start)
                last = layout.index_to_pos(start + len(value[:-1].encode()))
                left = min(rect.x, rect.x + rect.width, last.x, last.x + last.width) / Pango.SCALE
                right = max(rect.x, rect.x + rect.width, last.x, last.x + last.width) / Pango.SCALE
                row._action_geometry.append(
                    (
                        uri,
                        value,
                        origin.x + lx + left,
                        origin.y + ly + rect.y / Pango.SCALE,
                        max(1, round(right - left)),
                        max(1, round(rect.height / Pango.SCALE)),
                    )
                )
            if row._action_pending is not None:
                missing = True
                pending_rows.add(number)
            previous = row.body
            for uri, value, x, local_y, width, word_height in row._action_geometry:
                if top + local_y + word_height <= 0 or top + local_y >= height:
                    continue
                key = number, uri
                needed.add(key)
                button = self.buttons.get(key)
                if button is not None and button._word_turn != row._turn:
                    if button.get_parent():
                        button.unparent()
                    button = None
                if button is None:
                    if thread_time_ns() >= deadline:
                        missing = True
                        continue
                    button = Gtk.Button(
                        opacity=0,
                        can_target=False,
                        focusable=False,
                        accessible_role=Gtk.AccessibleRole.LINK,
                        css_classes=["transcript-word-action"],
                    )
                    button._word_turn = row._turn
                    button.update_property(
                        [Gtk.AccessibleProperty.LABEL, Gtk.AccessibleProperty.DESCRIPTION],
                        [value, description],
                    )
                    button.connect(
                        "clicked", lambda _button, turn=row._turn, uri=uri: view.page._word_clicked(turn, uri)
                    )
                    button.insert_after(row, previous)
                    self.buttons[key] = button
                # Existing action placement must not starve later rows of
                # their share of new parsing/geometry/creation work.
                placement_started = thread_time_ns()
                if button.get_parent() is not row:
                    if button.get_parent():
                        button.unparent()
                    button.insert_after(row, previous)
                if button.get_prev_sibling() is not previous:
                    button.insert_after(row, previous)
                previous = button
                button.allocate(
                    width, word_height, -1, Gsk.Transform.new().translate(Graphene.Point().init(x, local_y))
                )
                deadline += thread_time_ns() - placement_started

        for key in self.buttons.keys() - needed:
            button = self.buttons[key]
            if key[0] in pending_rows and button._word_turn == view.rows[key[0]]._turn:
                # Preserve accessible identity while replacement geometry yields.
                continue
            self.buttons.pop(key)
            if button.get_parent():
                button.unparent()

        if missing:
            self._retry()
