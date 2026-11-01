# D52.2 (P7) gap ranges and presence

Shared vectors for GNOME G6 and Mac M8. The rules are in SPEC.md, "Gap ranges and
presence". `gaps.py` is an independent oracle that reproduces every literal case. It is
not production code for either app. Each app replays the vectors through its own writer
and presence helper.

## writer.json

Each case lists abstract steps (frame counts only, PCM16 mono, 48 kHz unless `rate` is
given) and the expected recorded spans in creation order. `features` lists the negotiated
features: `[]` is a legacy session, which records ranges too. `selection` sets the initial
`me`/`others` state, and both default to true.

| op | Meaning |
| --- | --- |
| `track {id, kind=remote, rate}` | Browser wire declaration. Recorded now if its side is selected. |
| `audio {id, offsetMs, frames}` | Browser chunk, applying SPEC tolerance/padding. Discarded while deselected. |
| `series {id, firstOffsetMs, everyMs, count, frames}` | `count` audio chunks at `firstOffsetMs + i*everyMs`. |
| `select {atMs, me?, others?}` | App selection change. It ends spans; reselection opens a new span with boundary `atMs`. |
| `trackEnd {id, droppedFrames}` | Negotiated end, replayed with reason `ended` and endOffsetMs null. Adds the count to the latest span that recorded the wire ID. |
| `nativeTrack {id, kind, rate, boundaryMs?}` | Native span, with an optional selection boundary. |
| `input {id, hostMs, frames}` | Native input buffer with its session-relative host stamp. |

Each recorded span is closed at the end of the case, as by a user Stop without a problem.
Offsets round half up. A writer that keeps an inert zero-frame span for a source declared while
deselected leaves it out of the comparison; such a span recorded nothing and receives no drops.
Prefix cases follow S-6: a reselected or boundary-declared span starts at the boundary.

Expected tracks hold `wire`, `startOffsetMs`, `frames` (including padding), `gapFrames`,
`droppedFrames` and `gaps`. In the expected tracks, `gapFrames: 0` and `droppedFrames: 0`
mean absent or zero. `atMs` and `durationMs` compare within 1e-6 ms; `frames` must be equal.
The overflow case gives `gapsCount`, `gapsHead` and `gapsTail` instead of 1001 literal
entries. `state` and `problemKind` are the saved session's; `problemKind: null` means absent.
Native gap kinds assume an unwarped clock, so nominal and mapped positions agree.

## presence.json

Stored track facts (`startOffsetMs`, `sampleRate`, `frames`, optional `gaps`) plus optional
ownership windows go in; integer half-open 16 kHz session-sample ranges `present` and
`unknown` come out. A window [a,b) ms covers samples [ceil(16a - 1e-6), ceil(16b - 1e-6)).
A missing or null `gaps` is unknown. Presence never reads PCM and never uses `droppedFrames`.

`tests/test_gap_ranges.py` checks the oracle against every literal case. It also checks
that coalescing ranges, truncating without an overflow marker, storing drops only on the live
span, flagging every gap, or deriving the flag from stored ranges alone each fail at least one
vector. Not covered here (app fault injection): a long gap together with io or capture loss,
where io and capture take precedence, and a `writer` range for in-place host write padding.
