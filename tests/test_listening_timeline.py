# SPDX-License-Identifier: GPL-3.0-or-later
"""Readout intervals include silence, overlap and exact ends; snap in pixels."""

import pytest
from test_listen import turn

from cassini_talk.listening_timeline import snap_start, time_index


def test_overlap_returns_to_the_still_audible_voice_then_silence():
    turns = (turn(0, "ada", 1000, 5000), turn(1, "ben", 2000, 3000), turn(2, "cleo", 6000, 7000))
    assert time_index(turns) == ((1000, 2000, 3000, 5000, 6000, 7000), ("ada", "ben", "ada", None, "cleo", None))


@pytest.mark.parametrize("width", [100, 360, 1270])
def test_six_pixel_snap_is_independent_of_duration_and_width(width):
    turns = (turn(0, "ada", 1000, 1100), turn(1, "ada", 9000, 9500))
    mark = 10 + width * 0.1
    assert snap_start(turns, mark + 6, 10, width, 10000) == 1000
    assert snap_start(turns, mark - 6, 10, width, 10000) == 1000
    assert snap_start(turns, mark + 6.01, 10, width, 10000) is None
    assert snap_start((), mark, 10, width, 10000) is None


def test_dense_ticks_keep_the_exact_list_and_nearest_turn():
    turns = tuple(turn(i, "ada", i * 10, i * 10 + 5) for i in range(127))
    assert snap_start(turns, 12.6, 0, 100, 10000) == 1260
    assert snap_start(turns, 12.5, 0, 100, 10000) == 1250
    assert snap_start(turns, 100, 0, 0, 10000) is None


def test_real_gtk_ticks_do_not_change_scale_height_and_surface_pointer_snaps():
    from types import SimpleNamespace

    from gi.repository import Gdk, Graphene, Gtk
    from test_listen import until

    from cassini_talk.listening_timeline import ListeningTimeline

    if not Gtk.init_check():
        pytest.skip("GTK display unavailable")
    seeks = []
    page = SimpleNamespace(meeting=SimpleNamespace(speakers=[]), player=SimpleNamespace(position=0, seek=seeks.append))
    scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 20000, 1)
    scale.set_draw_value(False)
    timeline = ListeningTimeline(page, scale)
    root = Gtk.Box()
    root.append(Gtk.Label(label="Offset", width_request=100))
    root.append(timeline)
    window = Gtk.Window(child=root, default_width=800, default_height=80)
    window.present()
    try:
        until(lambda: scale.get_width() > 0, "scale allocation")
        height = scale.measure(Gtk.Orientation.VERTICAL, -1)
        turns = tuple(turn(i, "ada", 2000 + i * 100, 2050 + i * 100) for i in range(127))
        timeline.set_target(turns)
        assert scale.measure(Gtk.Orientation.VERTICAL, -1) == height
        snapshot = Gtk.Snapshot.new()
        timeline.draw_ticks(snapshot, scale.get_height())
        node = snapshot.to_node()
        assert node.get_n_children() == 127
        assert all(node.get_child(i).get_bounds().get_width() == 3 for i in range(127))
        assert all(node.get_child(i).get_bounds().get_height() == 8 for i in range(127))
        left, width = timeline._geometry()
        local = Graphene.Point()
        local.init(left + width * 0.1, 10)
        ok, surface = scale.compute_point(window, local)
        assert ok
        dx, dy = window.get_surface_transform()
        event = SimpleNamespace(
            get_event_type=lambda: Gdk.EventType.BUTTON_PRESS,
            get_button=lambda: 1,
            get_position=lambda: (True, surface.x - dx, surface.y - dy),
        )
        assert timeline._event(None, event)
        assert seeks == [2000]
        assert timeline._dragging and timeline._snapped
        # Starting a drag at a tick must not leave the scale stuck at the snap.
        local.init(left + width * 0.5, 10)
        ok, surface = scale.compute_point(window, local)
        assert ok
        motion = SimpleNamespace(
            get_event_type=lambda: Gdk.EventType.MOTION_NOTIFY,
            get_position=lambda: (True, surface.x - dx, surface.y - dy),
        )
        timeline._event(None, motion)
        assert seeks[-1] == 10000
        assert timeline._dragged
        timeline.set_target(())
        assert not timeline._event(None, event)
        assert seeks == [2000, 10000]
    finally:
        timeline.close()
        window.destroy()


def test_snap_uses_the_drawn_pixel_center_at_fractional_offsets():
    turns = (turn(0, "ada", 1290, 1400),)
    assert snap_start(turns, 19, 0, 100, 10000) == 1290  # drawn center = 13
    assert snap_start(turns, 19.01, 0, 100, 10000) is None
