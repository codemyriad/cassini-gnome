# SPDX-License-Identifier: GPL-3.0-or-later
"""Interaction contracts shared with the ordinary Meeting page."""

import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from cassini_talk.meeting_text import reading_turns, speaker_turns


def accessible(window, page, paint, until, baseline=False):
    # Keep GTK dispatching while the independent AT-SPI client makes calls.
    candidates = []
    for index, entry in page.bound.items():
        if not entry[2].get_mapped():
            continue
        if hasattr(page.list, "word_actions") and not any(key[0] == index for key in page.list.word_actions.buttons):
            continue
        ok, bounds = entry[2].compute_bounds(page.scroller)
        if ok and bounds.get_y() >= 0 and bounds.get_y() < page.scroller.get_height():
            candidates.append(index)
    turn, words, _row = page.bound[max(candidates)]
    word = next(w for w, shown in zip(turn.words, turn.items, strict=True) if shown)
    page._follow_until = GLib.get_monotonic_time() + 3600_000_000
    page.player.seek(word.start_ms + 1234)
    process = Gio.Subprocess.new(
        [
            sys.executable,
            str(Path(__file__).with_name("exact_accessible.py")),
            str(os.getpid()),
            words.get_text(),
            *(["baseline"] if baseline else []),
        ],
        Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE,
    )
    result = []
    process.communicate_utf8_async(None, None, lambda p, r: result.append(p.communicate_utf8_finish(r)))
    until(lambda: result)
    ok, output, error = result[0]
    assert ok and process.get_successful(), error or output
    assert json.loads(output)["links"] >= len(turn.words)
    if baseline:
        return json.loads(output)
    for _ in range(30):
        paint(window)
        if page.player.position == word.start_ms:
            break
    assert page.player.position == word.start_ms, (page.player.position, word.start_ms, output)
    paint(window)
    return json.loads(output)


def check_interactions(window, page, paint, until):
    model = page.meeting
    view = page.list
    result = {}
    # Real GtkScrolledWindow touch gestures pause follow without a replacement
    # controller or a claimed sequence. Both touch drag and swipe are observed.
    controllers = page.scroller.observe_controllers()
    for cls, signal in ((Gtk.GestureDrag, "drag-begin"), (Gtk.GestureSwipe, "swipe")):
        gestures = [
            controllers.get_item(i)
            for i in range(controllers.get_n_items())
            if isinstance(controllers.get_item(i), cls)
        ]
        assert gestures
        page._follow_until = 0
        gestures[0].emit(signal, 0.0, 0.0)
        assert page._follow_until > GLib.get_monotonic_time()
    result["touchFollowPause"] = True
    # Baseline GtkLabels are not selectable; do not promise whole-document copy.
    assert all(not words.get_selectable() for _, words, _ in page.bound.values())
    assert not hasattr(page, "search_entry"), "Meeting gained an inline search UI; add parity coverage"
    nav = view.navigation
    nav.request(5)
    paint(window)
    until(lambda: nav.focused() == 5)
    for key, delta in ((Gdk.KEY_Down, 1), (Gdk.KEY_Up, -1), (Gdk.KEY_KP_Down, 1), (Gdk.KEY_KP_Up, -1)):
        before = nav.focused()
        assert nav.key(None, key, 0, Gdk.ModifierType(0))
        paint(window)
        assert nav.focused() == before + delta
    for key in (Gdk.KEY_Page_Down, Gdk.KEY_KP_Page_Down, Gdk.KEY_Page_Up, Gdk.KEY_KP_Page_Up):
        assert nav.key(None, key, 0, Gdk.ModifierType(0))
        paint(window)
        assert window.get_focus() is not None
    assert not nav.key(None, Gdk.KEY_Down, 0, Gdk.ModifierType.ALT_MASK)
    for key, expected in (
        (Gdk.KEY_End, len(model.turns) - 1),
        (Gdk.KEY_Home, 0),
        (Gdk.KEY_KP_End, len(model.turns) - 1),
        (Gdk.KEY_KP_Home, 0),
    ):
        assert nav.key(None, key, 0, Gdk.ModifierType(0))
        paint(window)
        assert nav.focused() == expected
    # Arrow/link directions retain GTK's native label traversal.
    assert nav.key(None, Gdk.KEY_Right, 0, Gdk.ModifierType(0))
    nav.key(None, Gdk.KEY_Left, 0, Gdk.ModifierType(0))
    assert page._follow_until > GLib.get_monotonic_time()
    # A focused row survives scrolling out of the overscan window.
    nav.request(10)
    paint(window)
    focused = window.get_focus()
    view.scroll_to(200)
    paint(window)
    assert window.get_focus() is focused and 10 in view.rows
    # Child order stays chronological, including newly realized rows above.
    children = []
    child = view.get_first_child()
    while child:
        if hasattr(child, "_turn"):
            children.append(child._turn.index)
        child = child.get_next_sibling()
    assert children == sorted(children), children
    # Tab traversal materializes the next logical row at the virtual boundary.
    boundary = max(view.rows)
    nav.request(boundary)
    paint(window)
    for _ in range(160):
        view.child_focus(Gtk.DirectionType.TAB_FORWARD)
        paint(window)
        if nav.focused() is not None and nav.focused() > boundary:
            break
    assert nav.focused() == boundary + 1, (boundary, nav.focused())
    # The regular Xvfb job sends a real native keypad event too.
    if window.get_display().__gtype__.name == "GdkX11Display":
        import gi

        gi.require_version("GdkX11", "4.0")
        from gi.repository import GdkX11

        xid = str(GdkX11.X11Surface.get_xid(window.get_surface()))
        subprocess.run(["xdotool", "windowfocus", "--sync", xid, "key", "KP_Home"], check=True)
        until(lambda: nav.focused() == 0)
    result["keyboard"] = True
    # Timestamp and speaker name buttons invoke the existing production actions.
    view.scroll_to(10)
    paint(window)
    turn, words, row = page.bound[10]
    row.get_first_child().get_last_child().emit("clicked")
    assert page.player.playing and page.player.position == turn.start_ms
    page.player.pause()
    row.get_first_child().get_first_child().emit("clicked")
    assert window.listen.active and window.listen.speaker == turn.speaker
    window.listen.stop()
    page.player.pause()
    result["listen"] = True
    # A hand scroll suppresses follow, while an explicit resumed follow works.
    view.scroll_to(10)
    paint(window)
    page._hand_scroll()
    value = view._value()
    page.player.seek(model.turns[100].start_ms)
    paint(window)
    assert abs(view._value() - value) <= 1
    page._follow_until = 0
    page.player.seek(model.turns[101].start_ms)
    paint(window)
    assert 101 in page.bound
    # Timeline seek delegates to the page player and clears the manual pause.
    page.seek.emit("change-value", Gtk.ScrollType.JUMP, model.turns[20].start_ms)
    paint(window)
    assert page.player.position == model.turns[20].start_ms
    result["timelineFollow"] = True
    # Tail replacement/append and growth from an unchanged prefix preserve the
    # currently read row, its widget/focus and the pixel adjustment.
    nav.request(10)
    paint(window)
    anchor, widget, value = 10, view.rows[10], view._value()
    last = model.words[-1]
    end = max(word.end_ms for word in model.words)
    other = next(s.id for s in model.speakers if s.id != last.speaker)
    continued = replace(last, text="continued", start_ms=end + 10, end_ms=end + 110)
    appended = replace(last, speaker=other, text="Another", start_ms=end + 210, end_ms=end + 310)
    assert appended.end_ms < model.duration_ms
    new_words = (*model.words, continued, appended)
    growing = replace(
        model,
        transcript="running",
        eta_minutes=1,
        words=new_words,
        turns=reading_turns(new_words),
        listening_turns=speaker_turns(new_words),
        speakers=tuple(replace(s, talk_ms=s.talk_ms + 100) for s in model.speakers),
    )
    common = next(
        (i for i, (a, b) in enumerate(zip(model.turns, growing.turns, strict=False)) if a != b), len(model.turns)
    )
    expected_measurements = len(growing.turns) - common
    count = view.measured_rows
    action_key = next(key for key in view.word_actions.buttons if key[0] == anchor)
    stable_action = view.word_actions.buttons[action_key]
    page.update(growing)
    until(lambda: view.ready)
    paint(window)
    assert view.rows[anchor] is widget and abs(view._value() - value) <= 1
    assert view.measured_rows - count == expected_measurements, (
        "Unchanged rows were remeasured",
        view.measured_rows - count,
        expected_measurements,
    )
    assert view.word_actions.buttons[action_key] is stable_action
    assert window.get_focus() is widget
    # Updating the visible last row keeps its local vertical anchor too.
    nav.request(len(growing.turns) - 1)
    paint(window)
    tail_value = view._value()
    extended = replace(appended, text="again")
    tail_words = (*new_words[:-1], extended)
    growing = replace(
        growing, words=tail_words, turns=reading_turns(tail_words), listening_turns=speaker_turns(tail_words)
    )
    page.update(growing)
    until(lambda: view.ready)
    paint(window)
    assert abs(view._value() - tail_value) <= 1
    page.update(model)
    until(lambda: view.ready)
    paint(window)
    result["liveTail"] = True
    # Rename and themes affect header/inline chip labels without stale cache.
    nav.request(10)
    paint(window)
    owner = page.bound[10][0].speaker
    renamed = replace(
        model, speakers=tuple(replace(s, name="Renamed speaker") if s.id == owner else s for s in model.speakers)
    )
    page.update(renamed)
    until(lambda: view.ready)
    paint(window)
    assert "Renamed speaker" in page.bound[10][2].get_first_child().get_first_child().get_child().get_text()
    manager = Adw.StyleManager.get_default()
    scheme = manager.get_color_scheme()
    manager.set_color_scheme(Adw.ColorScheme.FORCE_DARK)
    until(lambda: view.ready)
    paint(window)
    assert manager.get_dark()
    manager.set_color_scheme(scheme)
    page.update(model)
    until(lambda: view.ready)
    paint(window)
    settings = view.get_settings()
    font = settings.get_property("gtk-font-name")
    before_font = view.measured_rows
    settings.set_property("gtk-font-name", "Cantarell 14")
    paint(window)
    until(lambda: view.ready)
    assert view.measured_rows > before_font
    for index, (_, _words, row) in page.bound.items():
        assert (
            row.measure(Gtk.Orientation.VERTICAL, view.layout_width).natural
            == view.prefix[index + 1] - view.prefix[index]
        )
    settings.set_property("gtk-font-name", font)
    paint(window)
    until(lambda: view.ready)
    result["renameThemeFont"] = True
    # Synthetic chip labels/punctuation all link to its original first atom.
    host = next(t for t in model.turns if t.asides)
    page._follow_until = 0
    page.player.seek(host.words[0].start_ms)
    paint(window)
    assert host.index in page.bound
    markup = page.bound[host.index][1].get_label()
    assert markup.count('href="aside:0:0"') >= 3
    for uri in ("aside:0:0", "aside:0:1"):
        page.bound[host.index][1].emit("activate-link", uri)
        expected = host.asides[0][1].words[int(uri[-1])].start_ms
        until(lambda expected=expected: page.player.position == expected)
    result["chipHits"] = True
    # A speaker present only in a folded reply stays in the actionable sidebar.
    reply = host.asides[0][1]
    indices = set(reply.word_indices)
    chip_words = tuple(replace(w, speaker="chip-only") if i in indices else w for i, w in enumerate(model.words))
    chip_model = replace(
        model,
        words=chip_words,
        turns=reading_turns(chip_words),
        listening_turns=speaker_turns(chip_words),
        speakers=(*model.speakers, replace(model.speakers[0], id="chip-only", name="Cara", colour_index=2)),
    )
    page.update(chip_model)
    until(lambda: view.ready)
    paint(window)
    assert "chip-only" in page._speaker_rows
    page._speaker_rows["chip-only"][1].emit("clicked")
    assert window.listen.active and window.listen.speaker == "chip-only"
    window.listen.stop()
    page.player.pause()
    page.update(model)
    until(lambda: view.ready)
    paint(window)
    result["chipOnlySpeaker"] = True
    # Actual remote accessibility Text/Hypertext and timed-link activation.
    nav.request(10)
    paint(window)
    page.player.pause()
    result["accessible"] = accessible(window, page, paint, until)
    return result
