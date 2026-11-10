# SPDX-License-Identifier: GPL-3.0-or-later
"""Named native link actions for visible words, alongside GtkLabel Text.

GTK's virtual GtkLabel hyperlink children expose ranges but no Action. These
non-targetable, transparent buttons supply real AT-SPI actions without changing
pointer handling or adding duplicate keyboard stops. Only visible words exist.
"""

from html.parser import HTMLParser

from gi.repository import Graphene, Gsk, Gtk, Pango

from .i18n import language, using_language
from .strings import text


class LinkSpans(HTMLParser):
    def __init__(self, markup):
        super().__init__(convert_charrefs=True)
        self.offset, self.current, self.spans = 0, None, []
        self.seen = set()
        self.feed(markup)

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.current = [dict(attrs)["href"], self.offset, [], False]
        elif tag == "b" and self.current:
            self.current[3] = True

    def handle_data(self, value):
        if self.current:
            self.current[2].append(value)
        self.offset += len(value.encode())

    def handle_endtag(self, tag):
        if tag == "a" and self.current:
            uri, start, values, synthetic_name = self.current
            value = "".join(values)
            if value.strip() and not synthetic_name and uri not in self.seen:
                self.spans.append((uri, start, value))
                self.seen.add(uri)
            self.current = None


class WordActions:
    def __init__(self, view):
        self.view, self.buttons = view, {}
        self.style = Gtk.CssProvider()
        self.style.load_from_string(".transcript-word-action { min-width: 0; min-height: 0; padding: 0; border: 0; }")

    def clear(self):
        for button in self.buttons.values():
            if button.get_parent():
                button.unparent()
        self.buttons.clear()

    def update(self, height):
        view, needed = self.view, set()
        with using_language(view.page._header_signature[-1] if view.page._header_signature else language()):
            description = text("player.playFromHere")
        if getattr(self, "description", None) != description:
            self.description = description
            for button in self.buttons.values():
                button.update_property([Gtk.AccessibleProperty.DESCRIPTION], [description])
        zero = Graphene.Point().init(0, 0)
        for number, row in view.rows.items():
            words = row.get_last_child()
            plain = words.get_text()
            if getattr(row, "_action_plain", None) != plain:
                row._action_plain = plain
                row._action_spans = LinkSpans(words.get_label()).spans
            ok, origin = words.compute_point(row, zero)
            if not ok:
                continue
            lx, ly = words.get_layout_offsets()
            layout = words.get_layout()
            previous = row.body
            for uri, start, value in row._action_spans:
                rect = layout.index_to_pos(start)
                y = view.prefix[number] - view._value() + origin.y + ly + rect.y / Pango.SCALE
                if y < 0 or y >= height:
                    continue
                key = number, uri
                needed.add(key)
                button = self.buttons.get(key)
                if button is not None and button._word_turn != row._turn:
                    if button.get_parent():
                        button.unparent()
                    button = None
                if button is None:
                    button = Gtk.Button(
                        opacity=0,
                        can_target=False,
                        focusable=False,
                        accessible_role=Gtk.AccessibleRole.LINK,
                        css_classes=["transcript-word-action"],
                    )
                    button.get_style_context().add_provider(self.style, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
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
                last = layout.index_to_pos(start + len(value[:-1].encode()))
                left = min(rect.x, rect.x + rect.width, last.x, last.x + last.width) / Pango.SCALE
                right = max(rect.x, rect.x + rect.width, last.x, last.x + last.width) / Pango.SCALE
                x = origin.x + lx + left
                local_y = origin.y + ly + rect.y / Pango.SCALE
                button.allocate(
                    max(1, round(right - left)),
                    max(1, round(rect.height / Pango.SCALE)),
                    -1,
                    Gsk.Transform.new().translate(Graphene.Point().init(x, local_y)),
                )
        for key in self.buttons.keys() - needed:
            button = self.buttons.pop(key)
            if button.get_parent():
                button.unparent()
