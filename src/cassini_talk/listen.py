# SPDX-License-Identifier: GPL-3.0-or-later
"""D70 listen by speaker: play one speaker's turns back to back.

The controller owns no audio of its own. It drives the window's Player
through play_span (an exact stop, signalled by the EOS the sink posts after
rendering it) and Player.span_follow, so a seek, a word click or ±10 s while
listening keeps listening on: inside a target turn playback stops at that
turn's end; elsewhere it plays freely until the next target turn, then
through it. Stepping always works relative to the playhead.
"""

from bisect import bisect_left, bisect_right

from gi.repository import GObject

LEAD_IN_MS = 1500
TAIL_MS = 500
# A turn that starts less than this after the speaker's previous turn ended
# gets no lead-in, and auto-advance runs straight on into it (D70.2).
CHAIN_GAP_MS = 2000


class ListenController(GObject.Object):
    """Listening state: target speaker, turn index and auto-advance.

    *player_source* returns the window's current Player; the window replaces
    it when a meeting needs the other player type, and stops listening then.
    """

    __gtype_name__ = "CassiniListenController"

    __gsignals__ = {
        # Target, index or finished changed (not emitted for position ticks).
        "changed": (GObject.SignalFlags.RUN_LAST, None, ()),
        "stopped": (GObject.SignalFlags.RUN_LAST, None, ()),
    }

    speaker = GObject.Property(type=str, default=None)
    index = GObject.Property(type=int, default=-1)
    count = GObject.Property(type=int, default=0)
    # The last turn has ended and nothing is left to auto-advance to ("Play again").
    finished = GObject.Property(type=bool, default=False)
    cued = GObject.Property(type=bool, default=False)

    def __init__(self, player_source):
        super().__init__()
        self._player_source = player_source
        self._player = None
        self._handlers = []
        self._by_speaker = {}
        self._targets = ()
        self._starts = []
        # Last turn index of the span the player is stopping at, or None while playing freely.
        self._span_last = None
        # The turn _play started (its lead-in belongs to it), or None after a
        # seek: a playhead in the gap before a turn has not reached that turn.
        self._started = None
        self._listening_position = None

    # Turn data

    def set_turns(self, turns):
        """All speakers' turns (meeting_text.speaker_turns, merges applied).

        Call again after a merge, an undo or a rename of a merge target. A
        target speaker that no longer has turns stops listening (returns False);
        otherwise the index is kept by playhead position.
        """
        by_speaker = {}
        for turn in turns:
            by_speaker.setdefault(turn.speaker, []).append(turn)
        self._by_speaker = {speaker: tuple(items) for speaker, items in by_speaker.items()}
        if not self.active:
            return True
        if self.speaker not in self._by_speaker:
            self.stop()
            return False
        self._set_target(self.speaker)
        # Recompute the stop and the index for the playhead, as a seek there would.
        self._player.seek(self._player.position)
        return True

    def turns_of(self, speaker):
        return self._by_speaker.get(speaker, ())

    @property
    def active(self):
        return self._player is not None and self.speaker is not None

    @property
    def turn(self):
        return self._targets[self.index] if self.active and 0 <= self.index < len(self._targets) else None

    # Starting and stopping

    def start(self, speaker, turn_index=None, *, play=True):
        """Listen to *speaker*, from *turn_index* or from its first turn not over before the playhead."""
        if not self._by_speaker.get(speaker):
            return False
        player = self._player_source()
        if player is None or not player.loaded:
            return False
        if player is not self._player:
            self._release()
            self._player = player
            self._handlers = [
                player.connect("notify::position", self._position_changed),
                player.connect("notify::playing", self._playing_changed),
                player.connect("error", lambda *_: self.stop()),
            ]
        self._set_target(speaker)
        if turn_index is None:
            ends = [turn.end_ms for turn in self._targets]
            turn_index = bisect_right(ends, player.position)
            if turn_index >= len(self._targets):
                turn_index = 0
        turn_index = max(0, min(len(self._targets) - 1, turn_index))
        if play:
            return self._play(turn_index)
        player.pause()
        self.cued = True
        self._started = turn_index
        self._listening_position = self.lead_start(turn_index)
        player.span_follow = self._follow
        player.seek(self._listening_position)
        self._update(turn_index, finished=False, force=True)
        return True

    def stop(self):
        """Stop listening; the position stays and playback (if any) goes on freely."""
        if self._player is None and self.speaker is None:
            return
        player = self._player
        self._release()
        if player is not None and player.span_follow == self._follow:
            player.clear_span()
        self._targets, self._starts, self._span_last, self._started = (), [], None, None
        self.freeze_notify()
        self.speaker, self.index, self.count, self.finished = None, -1, 0, False
        self.cued = False
        self.thaw_notify()
        self.emit("stopped")

    def _release(self):
        for handler in self._handlers:
            self._player.disconnect(handler)
        self._handlers = []
        self._player = None

    # Stepping

    def step_target(self, direction):
        """The turn index Previous (-1) or Next (+1) turn would play, or None at an end."""
        if not self.active:
            return None
        position = self._player.position
        current = self._current(position)
        if current is not None:
            target = current + direction
        elif direction > 0:
            target = bisect_right(self._starts, position)
        else:
            target = bisect_left(self._starts, position) - 1
        return target if 0 <= target < len(self._targets) else None

    def next_turn(self):
        target = self.step_target(1)
        return target is not None and self._play(target)

    def previous_turn(self):
        target = self.step_target(-1)
        return target is not None and self._play(target)

    def play_again(self):
        return self.active and self._play(0)

    def speaker_step_target(self, order, direction):
        """The speaker Previous (-1) or Next (+1) speaker would play, in pane *order*."""
        if not self.active:
            return None
        order = [speaker for speaker in order if self._by_speaker.get(speaker)]
        if self.speaker not in order:
            return None
        target = order.index(self.speaker) + direction
        return order[target] if 0 <= target < len(order) else None

    def next_speaker(self, order):
        target = self.speaker_step_target(order, 1)
        return target is not None and self.start(target, 0)

    def previous_speaker(self, order):
        target = self.speaker_step_target(order, -1)
        return target is not None and self.start(target, 0)

    # Spans

    def lead_start(self, index):
        turn = self._targets[index]
        if index > 0 and turn.start_ms - self._targets[index - 1].end_ms < CHAIN_GAP_MS:
            return turn.start_ms
        return max(0, turn.start_ms - LEAD_IN_MS)

    def chain_last(self, index):
        last = index
        while last + 1 < len(self._targets) and (
            self._targets[last + 1].start_ms - self._targets[last].end_ms < CHAIN_GAP_MS
        ):
            last += 1
        return last

    def span_end(self, last):
        end = self._targets[last].end_ms + TAIL_MS
        duration = self._player.duration if self._player is not None else 0
        return min(end, duration) if duration else end

    def _set_target(self, speaker):
        changed = speaker != self.speaker
        self._targets = self._by_speaker[speaker]
        self._starts = [turn.start_ms for turn in self._targets]
        self.freeze_notify()
        if changed:
            self.speaker = speaker
        self.count = len(self._targets)
        self.thaw_notify()

    def _play(self, index):
        self.cued = False
        last = self.chain_last(index)
        self._span_last = last
        self._started = index
        self._listening_position = self.lead_start(index)
        self._player.span_follow = self._follow
        self._update(index, finished=False, force=True)
        # A refusal (nothing loaded, or an error) returns False; the error path stops listening.
        return self._player.play_span(self.lead_start(index), self.span_end(last), self._span_done(last))

    def _span_done(self, last):
        def done(_end_ms):
            if not self.active or self._span_last != last:
                return
            if last + 1 < len(self._targets):
                self._play(last + 1)
            else:
                self._span_last = None
                self._update(last, finished=True)

        return done

    def _follow(self, ms):
        """Player.span_follow: where a seek to *ms* plays to while listening."""
        if not self.active:
            return None
        # A recompute or a dialog sample restore seeks to the saved listening
        # position. Keep ownership of its lead-in; a deliberate seek elsewhere
        # still follows the playhead rather than the previously started turn.
        started = self._started
        self._started = None
        if ms == self._listening_position and started is not None:
            index = bisect_right(self._starts, ms)
            if index < len(self._targets) and self.lead_start(index) <= ms < self._starts[index]:
                self._started = index
        self._listening_position = ms
        index = self._containing(ms)
        if index is None:
            index = bisect_right(self._starts, ms)
        if index >= len(self._targets):
            # Past the last turn: play freely; the bar keeps the last turn, and
            # Play again stays offered after it (a re-seek there, by a merge's
            # recompute or the dialog sample's restore, changes nothing).
            self._span_last = None
            self._update(len(self._targets) - 1, finished=self.finished)
            return None
        last = self.chain_last(index)
        self._span_last = last
        self._update(index, finished=False)
        return self.span_end(last), self._span_done(last)

    def _containing(self, ms):
        """The target turn whose [start, end + tail) holds *ms*, if any."""
        index = bisect_right(self._starts, ms) - 1
        if index >= 0 and ms < self.span_end(index):
            return index
        return None

    def _current(self, position):
        """The turn the playhead belongs to: the one _play started (its lead-in and
        tail included) or else the one holding the playhead."""
        if self.finished and self._targets and position == self.span_end(len(self._targets) - 1):
            return len(self._targets) - 1
        index = self._started
        if index is not None and (
            0 <= index <= self.index < len(self._targets)
            and self.lead_start(index) <= position <= self.span_end(self.index)
        ):
            return self.index if position >= self._starts[self.index] else index
        return self._containing(position)

    def _playing_changed(self, player, _pspec):
        if player.playing and self.cued:
            self.cued = False
            self.emit("changed")

    def _position_changed(self, player, _pspec):
        # Within a chain played as one span, the index follows the playhead.
        # Display only: the stop itself is exact and needs no position.
        if not self.active or player._range_end is not None:
            return
        self._listening_position = player.position
        if self._span_last is None or not player.playing:
            return
        index = bisect_right(self._starts, player.position) - 1
        if self.index < index <= self._span_last:
            self._update(index, finished=self.finished)

    def _update(self, index, *, finished, force=False):
        if not force and index == self.index and finished == self.finished:
            return
        self.freeze_notify()
        self.index = index
        self.finished = finished
        self.thaw_notify()
        self.emit("changed")
