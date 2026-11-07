# SPDX-License-Identifier: GPL-3.0-or-later
"""Listening-bar decisions, independent of widgets, GI and a display server."""

from dataclasses import dataclass

# Matches the approved staged collapse; the view supplies the toolkit units.
BREAKPOINTS = ((720, 1), (600, 2), (480, 3))


@dataclass(frozen=True)
class BarState:
    visible: bool
    previous_turn: bool
    next_turn: bool
    previous_speaker: bool
    next_speaker: bool
    edits_enabled: bool
    again_visible: bool
    again_label: str


def bar_state(listen, order, *, editable, busy):
    """Project the controller's playhead-relative targets, including index zero."""
    active = listen.active
    return BarState(
        visible=active,
        previous_turn=active and listen.step_target(-1) is not None,
        next_turn=active and listen.step_target(1) is not None,
        previous_speaker=active and listen.speaker_step_target(order, -1) is not None,
        next_speaker=active and listen.speaker_step_target(order, 1) is not None,
        edits_enabled=active and editable and not busy,
        again_visible=active and (listen.finished or listen.cued),
        again_label="listen.again" if listen.finished else "common.play",
    )


@dataclass(frozen=True)
class BarLayout:
    excerpt: bool
    edit_buttons: bool
    speaker_buttons: bool
    menu_actions: tuple[str, ...]


def bar_layout(stage, *, editable):
    return BarLayout(
        excerpt=stage == 0,
        edit_buttons=editable and stage < 2,
        speaker_buttons=stage < 3,
        menu_actions=(("name", "merge") if editable and stage >= 2 else ())
        + (("previous-speaker", "next-speaker") if stage >= 3 else ()),
    )


def merge_choices(meeting, turn):
    """Neighbours first, with copy IDs for the view; then remaining talk time."""
    others = {s.id: s for s in meeting.speakers if s.id != turn.speaker}
    neighbours = []
    for index, suffix in ((turn.index - 1, "speaksBefore"), (turn.index + 1, "speaksAfter")):
        if 0 <= index < len(meeting.listening_turns):
            speaker = others.pop(meeting.listening_turns[index].speaker, None)
            if speaker:
                neighbours.append((speaker, "speakers.mergeInto." + suffix))
    return neighbours, sorted(others.values(), key=lambda s: -s.talk_ms)


def excerpt(meeting, turn):
    before = meeting.listening_turns[turn.index - 1] if turn.index else None
    previous = " ".join(before.text.split()[-5:]) if before else ""
    current = " ".join(turn.text.split()[:8])
    return f"{previous} │ {current}" if previous else current
