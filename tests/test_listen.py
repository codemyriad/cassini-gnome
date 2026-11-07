# SPDX-License-Identifier: GPL-3.0-or-later
"""D70 step 2: Player.play_span and ListenController on real offline GStreamer.

The sink is an unsynced fakesink, so spans render as fast as GStreamer can
push them; what the sink received is recorded buffer by buffer. Every wait
blocks on the main loop until the bus event arrives (E1); the GLib timeout
is only a bound. Sample speakers are English (D70.1).
"""

import array
import pathlib
import threading
import wave
from dataclasses import replace

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

Gst.init(None)

from cassini_talk.folder_player import FolderPlayer  # noqa: E402
from cassini_talk.library import CaptureTrack  # noqa: E402
from cassini_talk.listen import LEAD_IN_MS, TAIL_MS, ListenController  # noqa: E402
from cassini_talk.meeting_text import SpeakerTurn  # noqa: E402
from cassini_talk.player import Player  # noqa: E402

RATE = 16000
DURATION_MS = 20000


def needs_playbin():
    if Gst.ElementFactory.find("playbin") is None:
        pytest.skip("GStreamer playbin unavailable")


def until(predicate, what, timeout_s=10):
    context = GLib.MainContext.default()
    expired = []
    source = GLib.timeout_add(timeout_s * 1000, lambda: expired.append(True) or GLib.SOURCE_REMOVE)
    try:
        while not predicate():
            assert not expired, f"timed out waiting for {what}"
            context.iteration(True)
    finally:
        if not expired:
            GLib.source_remove(source)


def wav(path, seconds=DURATION_MS // 1000):
    # A ramp, not silence, so every buffer carries real samples.
    samples = array.array("h", ((i * 7) % 8000 - 4000 for i in range(seconds * RATE)))
    with wave.open(str(path), "wb") as stream:
        stream.setparams((1, 2, RATE, 0, "NONE", "not compressed"))
        stream.writeframes(samples.tobytes())
    return seconds * RATE


class Rendered:
    """Contiguous stretches the sink rendered, in media milliseconds.

    clear() also drops whatever the sink still renders until the next flush:
    an unsynced sink can be ahead of a pause, so buffers of the old segment
    may still arrive before the seek that starts the stretch under test."""

    def __init__(self, sink):
        self.buffers = []
        self.waiting = False
        self.lock = threading.Lock()
        sink.set_property("sync", False)
        sink.set_property("signal-handoffs", True)
        sink.connect("handoff", self._handoff)
        sink.get_static_pad("sink").add_probe(Gst.PadProbeType.EVENT_FLUSH, self._flushed)

    def _handoff(self, _sink, buffer, _pad):
        with self.lock:
            if not self.waiting:
                self.buffers.append((buffer.pts, buffer.duration))

    def _flushed(self, _pad, info):
        with self.lock:
            if info.get_event().type == Gst.EventType.FLUSH_STOP:
                self.waiting = False
        return Gst.PadProbeReturn.OK

    def spans(self):
        spans = []
        with self.lock:
            buffers = tuple(self.buffers)
        for pts, duration in buffers:
            start, end = pts / Gst.MSECOND, (pts + duration) / Gst.MSECOND
            if spans and abs(spans[-1][1] - start) < 1:
                spans[-1][1] = end
            else:
                spans.append([start, end])
        return [(round(a), round(b)) for a, b in spans]

    def clear(self):
        with self.lock:
            self.waiting = True
            self.buffers.clear()


@pytest.fixture
def player(tmp_path):
    needs_playbin()
    path = tmp_path / "meeting.wav"
    wav(path)
    player = Player()
    sink = Gst.ElementFactory.make("fakesink", None)
    player._pipeline.set_property("audio-sink", sink)
    player.rendered = Rendered(sink)
    errors = []
    player.connect("error", lambda _player, message: errors.append(message))
    player.errors = errors
    player.load(path.as_uri(), DURATION_MS)
    until(lambda: player._prerolled or errors, "preroll")
    assert not errors
    player.rendered.clear()
    yield player
    player.shutdown()


def turn(index, speaker, start, end):
    return SpeakerTurn(index, speaker, (), (), start, end)


# Ada: two lone turns, then a chain of three (gaps under 2 s), then one near the end.
# Ben talks in between; Cleo once, right at the start.
TURNS = (
    turn(0, "cleo", 200, 900),
    turn(1, "ada", 3000, 3300),
    turn(2, "ben", 3400, 5000),
    turn(3, "ada", 7000, 7400),
    turn(4, "ben", 7500, 8000),
    turn(5, "ada", 9000, 9500),
    turn(6, "ben", 9600, 10000),
    turn(7, "ada", 11000, 11300),
    turn(8, "ben", 11400, 13500),
    turn(9, "ada", 14000, 14500),
    turn(10, "ben", 14600, 19000),
    turn(11, "ada", 19700, 19900),
)
ADA = [t for t in TURNS if t.speaker == "ada"]


def listening(player):
    controller = ListenController(lambda: player)
    controller.set_turns(TURNS)
    return controller


# Player.play_span


def test_play_span_stops_exactly_at_its_end_after_the_sink_rendered_it(player):
    done = []
    assert player.play_span(4321, 4621, done.append)
    until(lambda: done, "the span's EOS")
    assert done == [4621]
    assert player.rendered.spans() == [(4321, 4621)]
    assert not player.playing
    assert player.position == 4621
    assert not player._at_end
    assert player._pipeline.get_state(0)[1] == Gst.State.PAUSED


def test_play_span_has_no_ten_second_cap_and_keeps_its_position(player):
    done = []
    player.seek(1000)
    assert player.play_span(500, 12500, done.append)
    until(lambda: done, "the span's EOS")
    assert player.rendered.spans() == [(500, 12500)]
    assert player.position == 12500


def test_play_after_a_span_continues_from_its_end_to_the_end_of_the_file(player):
    done = []
    player.play_span(2000, 2400, done.append)
    until(lambda: done, "the span's EOS")
    player.rendered.clear()
    player.play()
    until(lambda: player._at_end, "the file's EOS")
    assert player.rendered.spans() == [(2400, DURATION_MS)]
    assert done == [2400]
    assert player.position == DURATION_MS


def test_a_span_past_the_end_is_clamped_and_then_play_starts_over(player):
    done = []
    player.play_span(19500, 25000, done.append)
    until(lambda: done, "the span's EOS")
    assert done == [DURATION_MS]
    assert player._at_end
    player.rendered.clear()
    player.play()
    until(lambda: player._at_end and not player.playing, "the file's EOS")
    assert player.rendered.spans()[0][0] == 0


def test_a_seek_inside_a_span_keeps_its_stop_and_one_past_it_plays_freely(player):
    done = []
    player.play_span(1000, 1500, done.append)
    player.pause()
    player.rendered.clear()
    player.seek(1200)
    player.play()
    until(lambda: done, "the span's EOS")
    assert player.rendered.spans()[-1] == (1200, 1500)
    player.rendered.clear()
    player.seek(3000)
    player.play()
    until(lambda: player._at_end, "the file's EOS")
    assert player.rendered.spans() == [(3000, DURATION_MS)]
    assert done == [1500]


def test_clearing_a_span_while_paused_drops_its_stop_for_the_next_play(player):
    player.play_span(1000, 1500)
    player.pause()
    player.clear_span()
    player.rendered.clear()
    player.play()
    until(lambda: player._at_end, "the file's EOS")
    assert player.rendered.spans()[-1][1] == DURATION_MS


@pytest.mark.parametrize("kind", ["file", "folder"])
def test_an_eos_still_queued_from_a_replaced_span_does_not_end_the_next(tmp_path, kind):
    needs_playbin()
    path = tmp_path / "meeting.wav"
    frames = wav(path, 8)
    sink = Gst.ElementFactory.make("fakesink", None)
    rendered = Rendered(sink)
    # Synced: a stale EOS taken for the new span's end would pause it before it is heard.
    sink.set_property("sync", True)
    if kind == "file":
        player = Player()
        player._pipeline.set_property("audio-sink", sink)
        player.load(path.as_uri(), 8000)
    else:
        player = FolderPlayer(audio_sink=sink)
        player.load_tracks([CaptureTrack(pathlib.Path(path), frames, RATE, 0, True)])
    errors = []
    player.connect("error", lambda _player, message: errors.append(message))
    try:
        until(lambda: player._prerolled or errors, "preroll")
        done = []
        player.play_span(1000, 1100, done.append)
        bus = player._pipeline.get_bus()
        # Take every message up to the EOS without dispatching any, and queue
        # them all again, the span's own ASYNC_DONE included: they are still
        # pending, in order, when the next span's seek is issued.
        pending = []
        while not pending or pending[-1].type != Gst.MessageType.EOS:
            message = bus.timed_pop(10 * Gst.SECOND)
            assert message is not None, "the first span's EOS"
            pending.append(message)
        assert any(m.type == Gst.MessageType.ASYNC_DONE for m in pending)
        for message in pending:
            bus.post(message)
        rendered.clear()
        player.play_span(5000, 5400, done.append)
        until(lambda: done or errors, "the new span's EOS")
        assert not errors
        assert done == [5400]
        assert rendered.spans() == [(5000, 5400)]
    finally:
        player.shutdown()


def test_play_range_keeps_its_cap_and_wins_over_a_span_then_hands_back(player):
    calls = []
    player.span_follow = lambda ms: calls.append(ms) or (ms + 300, None)
    assert player.play_range(1000, 15000)
    until(lambda: player._at_end, "the sample's EOS")
    assert player.rendered.spans() == [(1000, 11000)]
    assert calls == []
    player.pause()
    player.clear_range()
    player.rendered.clear()
    player.seek(5000)
    assert calls == [5000]
    player.play()
    until(lambda: not player.playing and player.position == 5300, "the followed span's EOS")
    assert player.rendered.spans() == [(5000, 5300)]


# ListenController


def test_turns_play_back_to_back_with_lead_in_tail_and_chains(player):
    controller = listening(player)
    changes = []
    controller.connect("changed", lambda c: changes.append((c.index, c.finished)))
    assert controller.start("ada")
    assert controller.count == 6
    until(lambda: controller.finished, "the last turn's end")
    assert player.rendered.spans() == [
        (3000 - LEAD_IN_MS, 3300 + TAIL_MS),
        # 7000 to 11300: three turns less than 2 s apart run straight on, one lead-in.
        (7000 - LEAD_IN_MS, 11300 + TAIL_MS),
        (14000 - LEAD_IN_MS, 14500 + TAIL_MS),
        # The tail clamps at the duration.
        (19700 - LEAD_IN_MS, DURATION_MS),
    ]
    assert controller.index == 5
    assert changes[0] == (0, False) and changes[-1] == (5, True)
    assert not player.playing
    assert player.position == DURATION_MS


def test_the_lead_in_clamps_at_the_start(player):
    controller = listening(player)
    controller.start("cleo")
    until(lambda: controller.finished, "Cleo's only turn")
    assert player.rendered.spans() == [(0, 900 + TAIL_MS)]


def test_stepping_into_a_chain_has_no_lead_in(player):
    controller = listening(player)
    controller.start("ada", 2)
    until(lambda: controller.index == 4 or controller.finished, "the chain's next span")
    assert player.rendered.spans()[0] == (9000, 11300 + TAIL_MS)


def test_starting_mid_meeting_begins_at_the_first_turn_not_yet_over(player):
    controller = listening(player)
    player.seek(8000)
    controller.start("ada")
    assert controller.index == 2
    controller.stop()
    player.seek(19950)
    controller.start("ada")
    # Nothing left after the playhead: from the first turn.
    assert controller.index == 0


def test_next_and_previous_turn_work_relative_to_the_playhead(player):
    controller = listening(player)
    controller.start("ada", 0)
    # While turn 0's lead-in plays, Next goes to turn 1, not turn 0 again.
    assert player.position == 3000 - LEAD_IN_MS
    assert controller.step_target(1) == 1
    assert controller.step_target(-1) is None
    player.pause()
    # A seek into the gap before a turn has not reached it: Next plays it,
    # also from that turn's lead-in window (a word clicked 1 s before it).
    player.seek(2000)
    assert controller.step_target(1) == 0
    player.seek(6000)
    assert controller.index == 1 and controller.step_target(1) == 1
    assert controller.step_target(-1) == 0
    # A playhead in Ben's long turn: the turns around it.
    player.seek(16000)
    assert controller.index == 5
    assert controller.step_target(-1) == 4
    assert controller.step_target(1) == 5
    player.seek(19950)
    assert controller.step_target(1) is None
    player.rendered.clear()
    assert controller.previous_turn()
    assert controller.index == 4
    until(lambda: controller.finished, "the last turn's end")
    assert player.rendered.spans() == [(14000 - LEAD_IN_MS, 14500 + TAIL_MS), (19700 - LEAD_IN_MS, DURATION_MS)]


def test_a_seek_into_a_turn_stops_at_its_end_and_into_a_gap_plays_through_the_next(player):
    controller = listening(player)
    controller.start("ada", 0)
    player.pause()
    player.rendered.clear()
    # Inside the last turn: stops at its end (clamped tail).
    player.seek(19800)
    player.play()
    until(lambda: controller.finished, "the turn's end")
    assert player.rendered.spans() == [(19800, DURATION_MS)]
    # Inside Ben's long turn: plays freely until Ada's next turn, then through it.
    player.rendered.clear()
    player.seek(16000)
    assert not controller.finished
    player.play()
    until(lambda: controller.finished, "the next turn's end")
    assert player.rendered.spans() == [(16000, DURATION_MS)]
    assert controller.index == 5


def test_a_seek_into_a_middle_turn_stops_at_its_end_and_a_gap_seek_plays_through_the_next(player):
    controller = listening(player)
    controller.start("ada", 0)
    player.pause()
    player.rendered.clear()
    player.seek(3100)  # Inside turn 0: its end, then the next chain with its lead-in.
    player.play()
    until(lambda: controller.index >= 2 or controller.finished, "the next chain")
    spans = player.rendered.spans()
    assert spans[0] == (3100, 3300 + TAIL_MS) and spans[1][0] == 7000 - LEAD_IN_MS
    player.pause()
    player.rendered.clear()
    player.seek(5000)  # In Ben's turn: freely up to Ada's 7000 chain, then through it.
    player.play()
    until(lambda: controller.index >= 4 or controller.finished, "the turn after the chain")
    assert player.rendered.spans()[0] == (5000, 11300 + TAIL_MS)


def test_a_seek_past_the_last_turn_plays_freely(player):
    controller = listening(player)
    controller.start("cleo")
    player.pause()
    player.rendered.clear()
    player.seek(5000)
    player.play()
    until(lambda: player._at_end, "the file's EOS")
    assert player.rendered.spans() == [(5000, DURATION_MS)]
    assert controller.active and controller.index == 0


def test_play_again_and_speaker_steps_in_pane_order(player):
    controller = listening(player)
    order = ["ben", "ada", "dora", "cleo"]
    controller.start("ada", 4)
    until(lambda: controller.finished, "Ada's last turn")
    assert controller.play_again() and controller.index == 0
    assert controller.speaker_step_target(order, 1) == "cleo"  # dora has no turns
    assert controller.speaker_step_target(order, -1) == "ben"
    player.pause()
    player.seek(15000)
    assert controller.next_speaker(order)
    assert controller.speaker == "cleo" and controller.index == 0 and controller.count == 1
    assert controller.speaker_step_target(order, 1) is None
    assert controller.previous_speaker(order) and controller.speaker == "ada"


def test_a_merge_keeps_the_index_by_playhead_and_a_merged_away_target_stops(player):
    controller = listening(player)
    stopped = []
    controller.connect("stopped", lambda _c: stopped.append(True))
    controller.start("cleo")
    player.pause()
    # Cleo merged into Ada: Ada gains a first turn; the playhead stays in it.
    merged = tuple(t if t.speaker != "cleo" else turn(t.index, "ada", t.start_ms, t.end_ms) for t in TURNS)
    assert not controller.set_turns(merged)
    assert stopped == [True] and not controller.active
    controller.set_turns(TURNS)
    controller.start("ada", 0)
    player.pause()
    player.seek(3100)
    assert controller.index == 0
    assert controller.set_turns(merged)
    assert controller.count == 7 and controller.index == 1


def test_stop_listening_keeps_the_position_and_plays_on_freely(player):
    controller = listening(player)
    stopped = []
    controller.connect("stopped", lambda _c: stopped.append(True))
    controller.start("ada", 2)
    player.pause()
    position = player.position
    controller.stop()
    assert stopped == [True]
    assert controller.speaker is None and controller.index == -1
    assert player.span_follow is None and player.position == position
    player.rendered.clear()
    player.play()
    until(lambda: player._at_end, "the file's EOS")
    assert player.rendered.spans() == [(position, DURATION_MS)]


def test_play_again_survives_a_recompute_and_a_dialog_sample_restore(player):
    controller = listening(player)
    controller.start("ada", 5)
    until(lambda: controller.finished, "Ada's last turn")
    assert controller.set_turns(TURNS) and controller.finished
    merged = tuple(replace(t, speaker="ben") if t.speaker == "cleo" else t for t in TURNS)
    assert controller.set_turns(merged) and controller.finished
    restore = player.position
    assert player.play_range(1000, 1500)
    until(lambda: not player.playing, "the dialog sample")
    player.pause()
    player.clear_range()
    player.seek(restore)
    assert controller.finished and controller.index == 5


@pytest.mark.parametrize("mode", ["span", "sample"])
def test_a_rate_change_at_a_drained_span_end_keeps_the_stop(player, mode):
    done = []
    if mode == "span":
        player.play_span(1000, 1100, done.append)
    else:
        assert player.play_range(1000, 1100)
    bus = player._pipeline.get_bus()
    # The span has rendered to its stop; its EOS is still waiting to be dispatched.
    eos = bus.timed_pop_filtered(10 * Gst.SECOND, Gst.MessageType.EOS)
    assert eos is not None
    bus.post(eos)
    player.set_rate(1.5)
    until(lambda: done if mode == "span" else player._at_end, "the span's EOS")
    assert done == ([1100] if mode == "span" else [])
    assert player.rendered.spans() == [(1000, 1100)]
    assert player.position == 1100 and not player.playing
    player.rendered.clear()
    if mode == "sample":
        player.clear_range()
        player.seek(1100)
    player.play()  # Continues from the stop, at the new rate.
    until(lambda: player._at_end, "the file's EOS")
    assert player.rendered.spans()[0][0] == 1100


def test_player_stop_and_errors_end_listening(player):
    controller = listening(player)
    controller.start("ada")
    player.emit("error", "The audio could not be played.")
    assert not controller.active


def test_folder_player_spans_cross_source_boundaries(tmp_path):
    path = tmp_path / "microphone.wav"
    frames = wav(path, 10)
    sink = Gst.ElementFactory.make("fakesink", None)
    rendered = Rendered(sink)
    player = FolderPlayer(audio_sink=sink)
    errors = []
    player.connect("error", lambda _player, message: errors.append(message))
    try:
        player.load_tracks(
            [
                CaptureTrack(pathlib.Path(path), frames, RATE, 0, True),
                CaptureTrack(pathlib.Path(path), frames, RATE, 8000, True),
            ]
        )
        until(lambda: player._prerolled or errors, "preroll")
        assert not errors and player.duration == 18000
        controller = ListenController(lambda: player)
        # Turn 0 starts in the first source only and ends in both; turn 1 runs past the first source's end.
        controller.set_turns((turn(0, "ada", 7600, 8400), turn(1, "ada", 9800, 10400), turn(2, "ben", 12000, 17000)))
        rendered.clear()
        controller.start("ada")
        until(lambda: controller.finished or errors, "the chain's end")
        assert not errors
        assert rendered.spans() == [(7600 - LEAD_IN_MS, 10400 + TAIL_MS)]
        assert player.position == 10900
    finally:
        player.shutdown()


@pytest.mark.parametrize("speaker,index,previous", [("ben", 1, 3), ("ada", 5, 4), ("cleo", 0, None)])
@pytest.mark.parametrize("restore", ["none", "recompute", "sample"])
def test_previous_after_finished_chain_or_restored_listening(player, speaker, index, previous, restore):
    controller = listening(player)
    controller.start(speaker, index)
    until(lambda: controller.finished, "the last listening span")
    if restore == "recompute":
        controller.set_turns(TURNS)
    elif restore == "sample":
        position = player.position
        assert player.play_range(1000, 1500)
        until(lambda: not player.playing, "dialog sample")
        player.clear_range()
        player.seek(position)
    assert controller.finished
    assert controller.step_target(-1) == previous
    assert controller.step_target(1) is None


@pytest.mark.parametrize("restore", ["recompute", "sample"])
def test_next_in_lead_in_after_listening_restore(player, restore, monkeypatch):
    controller = listening(player)
    # Keep the pipeline paused while entering the lead-in; no wall-clock race
    # with an unsynchronised sink that can render the whole turn immediately.
    with monkeypatch.context() as patch:
        patch.setattr(player, "play", lambda: None)
        controller.start("ada", 0)
    position = player.position
    assert position < ADA[0].start_ms
    if restore == "recompute":
        controller.set_turns(TURNS)
    else:
        assert player.play_range(7000, 7500)
        until(lambda: not player.playing, "dialog sample")
        player.clear_range()
        player.seek(position)
    assert controller.step_target(1) == 1
    assert controller.step_target(-1) is None


def test_dialog_sample_ticks_do_not_advance_listening_chain(player):
    controller = listening(player)
    controller.start("ben", 1)
    player.pause()
    assert player.play_range(15000, 15500)
    player.position = 15000
    assert controller.index == 1
    until(lambda: not player.playing, "dialog sample")
    assert controller.index == 1


@pytest.mark.parametrize("rate", [1.0, 1.5])
def test_finished_span_is_disarmed_and_pause_keeps_exact_end(player, rate):
    player.set_rate(rate)
    done = []
    player.play_span(1000, 1500, done.append)
    until(lambda: done, "span EOS")
    player.pause()
    assert player.position == 1500
    player.seek(500)
    player.play()
    until(lambda: player._at_end, "file EOS")
    assert done == [1500]
    assert player.position == DURATION_MS


def test_rate_change_after_non_unit_speed_sink_eos_does_not_replay_tail(player):
    player.set_rate(1.5)
    done = []
    player.play_span(1000, 1500, done.append)
    bus = player._pipeline.get_bus()
    eos = bus.timed_pop_filtered(10 * Gst.SECOND, Gst.MessageType.EOS)
    assert eos is not None
    before = tuple(player.rendered.buffers)
    bus.post(eos)
    player.set_rate(1.0)
    until(lambda: done, "queued span EOS")
    assert tuple(player.rendered.buffers) == before
    assert done == [1500]


@pytest.mark.parametrize("pause_pending", [False, True])
def test_prepreroll_span_seeks_before_rendering_and_pause_keeps_pending_position(player, tmp_path, pause_pending):
    path = tmp_path / "new.wav"
    wav(path, 2)
    player.load(path.as_uri())
    with player.rendered.lock:
        player.rendered.buffers.clear()
        player.rendered.waiting = False  # Include any unwanted audio before the seek's flush.
    done = []
    assert player.play_span(1000, 3000, done.append)
    assert player._pipeline.get_state(0)[2] != Gst.State.PLAYING
    if pause_pending:
        player.pause()
        assert player.position == 1000
        player.play()
    until(lambda: done or player.errors, "preroll and span EOS")
    assert not player.errors
    assert done == [2000]
    assert player.rendered.spans() == [(1000, 2000)]


def test_speed_change_after_seeking_away_from_finished_span_applies_to_new_segment(player):
    done = []
    player.play_span(1000, 1500, done.append)
    until(lambda: done, "span EOS")
    player.seek(5000)
    player.set_rate(1.5)
    assert not player._stale_segment
    player.play()
    until(lambda: player._at_end, "file EOS")
    assert done == [1500]


def test_cue_after_merge_never_starts_audio(player):
    controller = ListenController(lambda: player)
    controller.set_turns(TURNS)
    assert controller.start("ada", 1, play=False)
    assert controller.active and controller.index == 1
    assert not player.playing
    assert player.position == 5500
    assert controller.step_target(1) == 2
    assert controller.step_target(-1) == 0
    player.rendered.clear()
    player.play()
    until(lambda: controller.finished, "cue then play")
    assert player.position == 20000
    controller.stop()


def test_dialog_sample_preserves_the_paused_cue_and_restores_its_position(player):
    controller = ListenController(lambda: player)
    controller.set_turns(TURNS)
    assert controller.start("ada", 1, play=False)
    saved = player.position
    assert controller.cued
    assert player.play_range(100, 800)
    until(lambda: not player.playing, "dialog sample end")
    assert controller.cued and controller.index == 1
    player.clear_range()
    player.seek(saved)
    assert not player.playing and player.position == saved
    assert controller.cued and controller.step_target(1) == 2
    controller.stop()
