# SPDX-License-Identifier: GPL-3.0-or-later
"""Named native link actions for visible words, alongside GtkLabel Text.

GTK's virtual GtkLabel hyperlink children expose ranges but no Action. These
non-targetable, transparent buttons supply real AT-SPI actions without changing
pointer handling or adding duplicate keyboard stops. Only visible words exist.
"""

import re
import time
from html import unescape

from gi.repository import GLib, Graphene, Gsk, Gtk, Pango

from .i18n import language, using_language
from .strings import text

# This consumes only our escaped, canonical reading_markup output. It avoids
# importing/general-purpose HTML parsing on the first transcript opening.
_TOKENS = re.compile(r"<[^>]*>|[^<]+")


class LinkSpans:
    def __init__(self, markup):
        offset, current, seen = 0, None, set()
        self.spans = []
        for match in _TOKENS.finditer(markup):
            token = match[0]
            if token.startswith('<a href="'):
                current = [token[9:-2], offset, [], False]
            elif token == "<b>" and current:
                current[3] = True
            elif token == "</a>" and current:
                uri, start, parts, synthetic = current
                value = "".join(parts)
                if value.strip() and not synthetic and not uri.endswith(":anchor") and uri not in seen:
                    self.spans.append((uri, start, value))
                    seen.add(uri)
                current = None
            elif not token.startswith("<"):
                value = unescape(token)
                if current:
                    current[2].append(value)
                offset += len(value.encode())


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
        deadline = time.thread_time_ns() + 2_000_000
        missing = False
        with using_language(view.page._header_signature[-1] if view.page._header_signature else language()):
            description = text("player.playFromHere")
        if getattr(self, "description", None) != description:
            self.description = description
            for button in self.buttons.values():
                button.update_property([Gtk.AccessibleProperty.DESCRIPTION], [description])
        zero = Graphene.Point().init(0, 0)
        for number, row in view.rows.items():
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
                layout = words.get_layout()
                geometry = []
                for uri, start, value in LinkSpans(words.get_label()).spans:
                    rect = layout.index_to_pos(start)
                    last = layout.index_to_pos(start + len(value[:-1].encode()))
                    left = min(rect.x, rect.x + rect.width, last.x, last.x + last.width) / Pango.SCALE
                    right = max(rect.x, rect.x + rect.width, last.x, last.x + last.width) / Pango.SCALE
                    geometry.append(
                        (
                            uri,
                            value,
                            origin.x + lx + left,
                            origin.y + ly + rect.y / Pango.SCALE,
                            max(1, round(right - left)),
                            max(1, round(rect.height / Pango.SCALE)),
                        )
                    )
                row._action_geometry_key, row._action_geometry = geometry_key, geometry
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
                    if time.thread_time_ns() >= deadline:
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

        for key in self.buttons.keys() - needed:
            button = self.buttons.pop(key)
            if button.get_parent():
                button.unparent()

        if missing:
            self._retry()
