# SPDX-License-Identifier: GPL-3.0-or-later
"""Every painted rebuild frame preserves the current viewport and manual input."""

from bisect import bisect_right

from gi.repository import Gtk


def check_rebuild(window, page, paint, until, native_scroll):
    view = page.list
    adjustment = view.props.vadjustment
    middle = len(view.items) // 2
    adjustment.set_value(view.prefix[middle] + 7)
    paint(window)
    expected = [middle, -7]
    samples = []
    failures = []

    def observe(*_):
        index = bisect_right(view.prefix, view._value()) - 1
        row = view.rows.get(index)
        offset = view.prefix[index] - view._value()
        valid = row is not None and row.get_child_visible()
        y = None
        if valid:
            valid, bounds = row.compute_bounds(page.scroller)
            y = bounds.get_y() if valid else None
        sample = (index, offset, y, adjustment.get_upper(), view.layout_width)
        samples.append(sample)
        if index != expected[0] or abs(offset - expected[1]) > 1 or not valid or abs(y - offset - 22) > 1:
            failures.append((tuple(expected), sample))

    clock = window.get_frame_clock()
    handler = clock.connect("after-paint", observe)
    try:
        previous = view._active
        upper = adjustment.get_upper()
        retained = dict(view.rows)
        view.invalidate()
        paint(window)
        assert view._active is previous and view._building is not previous
        assert adjustment.get_upper() == upper
        assert all(view.rows.get(i) is row for i, row in retained.items())
        probe = view._probe
        assert probe is not None and not probe.get_child_visible() and not probe.get_focusable()
        assert Gtk.test_accessible_has_state(probe, Gtk.AccessibleState.HIDDEN)
        # Exercise the same follow-pause hook used by input capture, followed
        # by GTK's native continuous scroll handler while the old index displays.
        _, bubble, capture = native_scroll(page)
        before = view._value()
        page._hand_scroll()
        capture.emit("scroll-begin")
        bubble.emit("scroll-begin")
        if not capture.emit("scroll", 0, 0.1):
            bubble.emit("scroll", 0, 0.1)
        bubble.emit("scroll-end")
        assert view._value() > before
        expected[:] = [bisect_right(view.prefix, view._value()) - 1, 0]
        expected[1] = view.prefix[expected[0]] - view._value()
        paint(window)
        assert view._active is previous
        until(lambda: view._active is not previous)
        assert not view.ready, "Replacement waited for the entire transcript"
        paint(window)
        assert not failures, failures[:5]
        # Abandon this still-partial active index with another uncached width.
        # Every frame must retain the viewport while its successor catches up.
        previous = view._active
        window.set_default_size(860, window.get_default_size()[1])
        until(lambda: view._building is not None and view._building is not previous)
        assert view._active is previous
        until(lambda: view._active is not previous)
        assert not view.ready
        measured = len(view.prefix)
        paint(window)
        assert len(view.prefix) > measured, "Replacement stopped publishing measured rows"
        assert not failures, failures[:5]
    finally:
        clock.disconnect(handler)
    assert len(samples) > 4
    return len(samples)
