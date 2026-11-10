# Copyright 2026 Codemyriad OÜ
# SPDX-License-Identifier: GPL-3.0-or-later
"""Actual Pango runs and pixels, including a paused page's style changes."""

from types import SimpleNamespace

from gi.repository import Adw, Gsk, Gtk

from cassini_talk.markup_runs import bridge_link_spaces
from cassini_talk.meeting_text import reading_turns
from cassini_talk.portable import Word


def check_markup_runs(window, until):
    page = window.meeting_page
    speaker = page.meeting.speakers[0].id
    turn = reading_turns([Word(speaker, i * 100 + 100, i * 100 + 190, f"word{i}") for i in range(50)])[0]
    turn.index = key = max(page.bound, default=0) + 1000
    probe = Gtk.Window(default_width=700, transient_for=window)
    container = Gtk.Box(css_classes=["meeting-transcript"])
    probe.set_child(container)
    page._bind(None, SimpleNamespace(get_item=lambda: SimpleNamespace(turn=turn), set_child=container.append))
    _, words, _ = page.bound[key]
    words.add_css_class("glyph-run-probe")
    manager = Adw.StyleManager.get_default()
    old_position, old_scheme = page.player.position, manager.get_color_scheme()
    provider = Gtk.CssProvider()
    Gtk.StyleContext.add_provider_for_display(window.get_display(), provider, 900)

    def frame():
        painted = []
        clock = probe.get_frame_clock()
        signal = clock.connect("after-paint", lambda *_: painted.append(True))
        try:
            probe.queue_draw()
            until(probe, lambda: bool(painted) and page._style_source == 0)
        finally:
            clock.disconnect(signal)

    def joined():
        c = words.get_color()
        return bridge_link_spaces(page._turn_markup(turn), (c.red, c.green, c.blue, c.alpha))

    def capture():
        snap = Gtk.Snapshot.new()
        Gtk.WidgetPaintable.new(words).snapshot(snap, words.get_width(), words.get_height())
        node = snap.to_node()
        assert node is not None

        def count(node):
            if isinstance(node, Gsk.TextNode):
                return 1
            if isinstance(node, Gsk.ContainerNode):
                return sum(count(node.get_child(i)) for i in range(node.get_n_children()))
            return count(node.get_child()) if hasattr(node, "get_child") else 0

        # Both images use Gdk's same lossless PNG encoder, including alpha.
        texture = probe.get_renderer().render_texture(node, node.get_bounds())
        return (
            count(node),
            (texture.get_width(), texture.get_height()),
            texture.save_to_png_bytes().get_data(),
            words.get_layout().get_pixel_size(),
        )

    try:
        probe.present()
        until(probe, words.get_mapped)
        for scheme in (Adw.ColorScheme.FORCE_LIGHT, Adw.ColorScheme.FORCE_DARK, Adw.ColorScheme.FORCE_LIGHT):
            manager.set_color_scheme(scheme)
            frame()
            # No manual render: the real page's deferred style handler must
            # replace the previous theme's separator attributes while paused.
            assert words.get_label() == joined()
            for position in (0, 2600, 5100):
                page.player.position = position
                raw = page._turn_markup(turn)
                words.set_markup(raw)
                frame()
                before = capture()
                page._render_words(turn, words)
                frame()
                after = capture()
                assert after[0] < before[0] / 2, (manager.get_dark(), position, before[0], after[0])
                assert before[1:] == after[1:], (manager.get_dark(), position)
                print("PASS glyph runs", manager.get_dark(), position, before[0], after[0], flush=True)
        # Force an opaque high-contrast foreground and send its independent
        # style notification; dark does not change in this transition.
        provider.load_from_string("label.glyph-run-probe { color: black; }")
        manager.notify("high-contrast")
        frame()
        assert words.get_color().alpha == 1.0 and words.get_label() == joined(), (
            words.get_color(),
            words.get_label(),
            joined(),
        )
        words.set_markup(page._turn_markup(turn))
        frame()
        before = capture()
        page._render_words(turn, words)
        frame()
        after = capture()
        assert after[0] < before[0] / 2 and before[1:] == after[1:]
        print("PASS glyph runs high contrast", before[0], after[0], flush=True)
    finally:
        page.bound.pop(key, None)
        probe.destroy()
        Gtk.StyleContext.remove_provider_for_display(window.get_display(), provider)
        page.player.position = old_position
        manager.set_color_scheme(old_scheme)
