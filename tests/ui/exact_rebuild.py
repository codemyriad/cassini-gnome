# SPDX-License-Identifier: GPL-3.0-or-later
"""Every painted rebuild frame preserves the current viewport and manual input."""

from bisect import bisect_right
from dataclasses import replace
from time import thread_time_ns

from gi.repository import Gtk

from cassini_talk.meeting_text import reading_turns, speaker_turns


def check_rebuild(window, page, paint, until, native_scroll):
    view = page.list
    adjustment = view.props.vadjustment
    model = page.meeting
    last = model.words[-1]
    words = (*model.words, replace(last, text="continued", start_ms=last.end_ms + 1000, end_ms=last.end_ms + 1500))
    grown = replace(model, words=words, turns=reading_turns(words), listening_turns=speaker_turns(words))
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
        # Replace the live tail while a deep replacement is still incomplete,
        # both with and without input after the model update.
        for hand_scroll in (False, True):
            until(lambda: view.ready)
            previous = view._active
            view.invalidate()
            paint(window)
            assert view._active is previous and view._building is not None
            page.update(grown)
            paint(window)
            assert view._active is previous and not view.ready
            if hand_scroll:
                page._hand_scroll()
                capture.emit("scroll-begin")
                bubble.emit("scroll-begin")
                if not capture.emit("scroll", 0, 1.5):
                    bubble.emit("scroll", 0, 1.5)
                bubble.emit("scroll-end")
                expected[0] = bisect_right(view.prefix, view._value()) - 1
                expected[1] = view.prefix[expected[0]] - view._value()
            until(lambda: view.ready)
            paint(window)
            assert not failures, failures[:5]
            page.update(model)
            until(lambda: view.ready)
            paint(window)
            assert not failures, failures[:5]
    finally:
        clock.disconnect(handler)
    assert len(samples) > 4
    return len(samples)


def check_pending_focus(window, model, paint, until):
    window.back_to_library()
    window.open_meeting(model, external=True)
    page = window.meeting_page
    view = page.list
    assert len(view.prefix) == 1
    view.invalidate()
    assert not view._display_dirty, "An empty displayed index needs no geometry refresh"
    paint(window)
    assert not view.ready
    target = len(view.items) - 5
    assert target + 1 >= len(view.prefix)
    assert view.navigation.request(target)
    last = model.words[-1]
    words = (*model.words, replace(last, text="continued", start_ms=last.end_ms + 1000, end_ms=last.end_ms + 1500))
    grown = replace(model, words=words, turns=reading_turns(words), listening_turns=speaker_turns(words))
    page.update(grown)
    until(lambda: view.navigation.focused() == target)
    paint(window)
    row = view.rows[target]
    ok, bounds = row.compute_bounds(page.scroller)
    assert ok and bounds.get_y() >= 0 and bounds.get_y() + bounds.get_height() <= page.scroller.get_height(), (
        "Surviving keyboard request focused an offscreen row",
        target,
        bounds.get_y(),
        bounds.get_height(),
        page.scroller.get_height(),
    )
    assert view.navigation.pending is None and view._pending_scroll is None
    return True


def check_presentation(window, page, model, paint, until, native_scroll, profile=None):
    """Real metric changes must agree with retained allocations on every frame."""
    view = page.list
    until(lambda: view.ready)
    deep = next(turn.index for turn in view.items[len(view.items) // 2 :] if turn.asides)
    view.props.vadjustment.set_value(view.prefix[deep] + 7)
    paint(window)
    expected = [deep, 7]
    failures = []
    samples = []
    frame_start = [0]
    fast_cpu = []
    input_pixels = 0
    fast_frames = [False]
    rebuilding_frames = [0]

    def begin_frame(*_):
        frame_start[0] = thread_time_ns()

    def observe(*_):
        if fast_frames[0]:
            rebuilding_frames[0] += not view.ready
            fast_cpu.append((thread_time_ns() - frame_start[0]) / 1e6)
        index = bisect_right(view.prefix, view._value()) - 1
        offset = view._value() - view.prefix[index]
        sample = (index, offset)
        samples.append(sample)
        if index != expected[0] or abs(offset - expected[1]) > 1:
            failures.append(("anchor", tuple(expected), sample))
        for number in view.visible_rows:
            row = view.rows[number]
            natural = row.measure(Gtk.Orientation.VERTICAL, view.layout_width).natural
            allocated = view.prefix[number + 1] - view.prefix[number]
            if natural != allocated:
                failures.append(("height", number, natural, allocated))

    def rebuild(change, *, fast=False):
        nonlocal input_pixels
        old = view._active
        change()
        paint(window)
        assert view._active is old, "Fixture did not exercise retained geometry"
        _, bubble, capture = native_scroll(page)
        page._hand_scroll()
        capture.emit("scroll-begin")
        bubble.emit("scroll-begin")
        if not capture.emit("scroll", 0, 0.1):
            bubble.emit("scroll", 0, 0.1)
        bubble.emit("scroll-end")
        index = bisect_right(view.prefix, view._value()) - 1
        expected[:] = index, view._value() - view.prefix[index]
        if fast:
            fast_frames[0] = True
            # Sustained finger-down input visits fresh rows as font metrics
            # change; reverse direction while the replacement is incomplete.
            for direction in (1, -1):
                capture.emit("scroll-begin")
                bubble.emit("scroll-begin")
                for _ in range(24):
                    before_value = view._value()
                    if not capture.emit("scroll", 0, direction * 3.0):
                        bubble.emit("scroll", 0, direction * 3.0)
                    input_pixels += abs(view._value() - before_value)
                    index = bisect_right(view.prefix, view._value()) - 1
                    expected[:] = index, view._value() - view.prefix[index]
                    paint(window)
                bubble.emit("scroll-end")
            fast_frames[0] = False
        until(lambda: view.ready)
        paint(window)
        assert not failures, failures[:8]

    clock = window.get_frame_clock()
    handler = clock.connect("after-paint", observe)
    before_handler = clock.connect("before-paint", begin_frame)
    provider = Gtk.CssProvider()
    provider.load_from_data(b".meeting-transcript .words { font-size: 22px; }")
    display = window.get_display()
    before = view.prefix[deep + 1] - view.prefix[deep]
    try:

        def enlarge():
            Gtk.StyleContext.add_provider_for_display(display, provider, Gtk.STYLE_PROVIDER_PRIORITY_USER)
            view.invalidate()

        rebuild(enlarge, fast=True)
        assert view.prefix[deep + 1] - view.prefix[deep] > before
        rebuild(lambda: (Gtk.StyleContext.remove_provider_for_display(display, provider), view.invalidate()))
        before = view.prefix[deep + 1] - view.prefix[deep]
        renamed = replace(
            model,
            speakers=tuple(
                replace(s, name=("A considerably longer synthetic speaker name " * 12)) for s in model.speakers
            ),
        )
        rebuild(lambda: page.update(renamed))
        assert view.prefix[deep + 1] - view.prefix[deep] > before
        rebuild(lambda: page.update(model))
        # Speaker headers and inline chips use different real GTK style paths.
        # Chips share the words label; there is no separate .speaker widget.
        view.props.vadjustment.set_value(view.prefix[deep] + 7)
        expected[:] = deep, 7
        paint(window)
        speaker_style = Gtk.CssProvider()
        speaker_style.load_from_data(
            b".meeting-transcript .transcript-name { font-size: 32px; min-height: 320px; }"
            b".meeting-transcript .words { font-size: 21px; letter-spacing: 0.8px; }"
        )
        before = view.prefix[deep + 1] - view.prefix[deep]
        try:
            rebuild(
                lambda: (
                    Gtk.StyleContext.add_provider_for_display(display, speaker_style, Gtk.STYLE_PROVIDER_PRIORITY_USER),
                    view.invalidate(),
                )
            )
            assert view.prefix[deep + 1] - view.prefix[deep] > before
            assert view.rows[deep].get_first_child().measure(Gtk.Orientation.VERTICAL, 120).natural >= 320
            rebuild(lambda: (Gtk.StyleContext.remove_provider_for_display(display, speaker_style), view.invalidate()))
        finally:
            Gtk.StyleContext.remove_provider_for_display(display, speaker_style)
    finally:
        clock.disconnect(handler)
        clock.disconnect(before_handler)
        Gtk.StyleContext.remove_provider_for_display(display, provider)
    assert len(fast_cpu) >= 48 and rebuilding_frames[0] > 0 and input_pixels > 1000
    if profile is not None:
        profile.update(
            frames=len(fast_cpu),
            rebuildingFrames=rebuilding_frames[0],
            worstFrameCpuMs=max(fast_cpu),
            inputPixels=input_pixels,
        )
    return len(samples)
