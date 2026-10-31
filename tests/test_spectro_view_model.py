# SPDX-License-Identifier: GPL-3.0-or-later
"""Injected session clocks cover scroll delay and distinct source states."""

from cassini_talk.spectro_view_model import HOLE, OFF, SILENT, Delay, DisplayModel


def test_delay_grows_at_most_half_a_second_per_second_and_shrinks_at_two_percent():
    clock = Delay()
    assert clock.advance(0) == 40
    clock.arrived(1000, 0)
    assert clock.advance(1000) == 540
    assert clock.advance(2000) == 1020
    assert clock.advance(5001) == 959.98
    assert 5001 - clock.value > 2000 - 1020


def test_data_is_released_at_the_delayed_clock_and_late_cells_are_not_rewritten():
    model = DisplayModel()
    model.advance(0)
    model.ingest(40, dict(me=[(2, 0, bytes([17]) * 32)], others=[]))
    model.advance(79)
    assert model.cell("me", 2) == (HOLE, None)
    model.advance(80)
    assert model.cell("me", 2) == (0, bytes([17]) * 32)
    model.ingest(100, dict(me=[(2, 0, bytes([99]) * 32)], others=[]))
    assert model.late == 1 and model.cell("me", 2)[1] == bytes([17]) * 32


def test_silence_holes_off_and_mic_warning_are_independent_states():
    model = DisplayModel()
    model.set_events([dict(type="selection", atMs=0, me=True, others=False)])
    model.set_warning(0, True)
    model.advance(0)
    model.ingest(20, dict(me=[(1, 0, bytes(32))], others=[]))
    model.advance(60)
    assert model.cell("me", 1) == (SILENT, bytes(32))
    assert model.cell("me", 0) == (HOLE, None)
    assert model.cell("others", 1) == (OFF, None)


def test_pill_pools_four_times_and_two_bands_by_max_instead_of_average():
    model = DisplayModel()
    model.advance(0)
    rows = []
    for slot in range(4):
        values = bytearray(32)
        values[slot] = 100 + slot
        rows.append((slot, 0, bytes(values)))
    model.ingest(20, dict(me=rows, others=[]))
    model.advance(100)
    flags, values = model.pill_cell("me", 0)
    assert flags == 0 and values[:2] == bytes([101, 103]) and not any(values[2:])
    assert model.latest_pill == 0


def test_clock_leap_and_never_released_future_inputs_keep_storage_bounded():
    model = DisplayModel()
    sizes = [(len(p.tags), len(p.bands)) for p in model.pending.values()]
    for slot in range(10000):
        model.ingest(slot * 20, dict(me=[(slot + 100000, 0, bytes(32))], others=[]))
    released = model.advance(100000000)
    assert len(released) == model.CAPACITY
    assert sizes == [(len(p.tags), len(p.bands)) for p in model.pending.values()]
    assert model.delay.count <= 320 and model.pending["me"].overflow > 0


def test_maximum_selection_history_stays_compact_and_retains_every_choice():
    import sys

    model = DisplayModel()
    model.set_events([dict(type="selection", atMs=index * 20, me=True, others=index % 2 == 0) for index in range(1000)])
    assert sys.getsizeof(model.selections) + sys.getsizeof(model.selection_times) < 10000
    for index in range(1000):
        assert not model._state("me", index) & OFF
        assert bool(model._state("others", index) & OFF) == (index % 2 == 1)
