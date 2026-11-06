# D52.2 (P7) gap ranges and presence

Shared vectors for GNOME G6 and Mac M8. The rules are in SPEC.md, "Gap ranges and
presence". `gaps.py` is an independent oracle that reproduces every literal case. It is
not production code for either app. Each app replays the vectors through its own writer
and presence helper.

## writer.json

Each case lists abstract steps (frame counts only, PCM16 mono, 48 kHz unless `rate` is
given) and the expected recorded spans in creation order. `features` lists the negotiated
features: `[]` is a legacy session, which records ranges too. `selection` sets the initial
`me`/`others` state, and both default to true. Optional `problem` is an earlier problem
that the writer-loss message must follow without replacing it.

| op | Meaning |
| --- | --- |
| `track {id, kind=remote, rate}` | Browser wire declaration. Recorded now if its side is selected. |
| `audio {id, offsetMs, frames}` | Browser chunk, applying SPEC tolerance/padding. Discarded while deselected. |
| `series {id, firstOffsetMs, everyMs, count, frames}` | `count` audio chunks at `firstOffsetMs + i*everyMs`. |
| `select {atMs, me?, others?}` | App selection change. It ends spans; reselection opens a new span with boundary `atMs`. |
| `trackEnd {id, droppedFrames}` | Negotiated end, replayed with reason `ended` and endOffsetMs null. Adds the count to the latest span that recorded the wire ID. |
| `nativeTrack {id, kind, rate, boundaryMs?}` | Native span, with an optional selection boundary. |
| `input {id, hostMs, frames, writerFrames=0, inferredFrames=0}` | Native surviving input buffer. After a preceding `input`, `writerFrames` is the exact count of admitted callback frames dropped by the writer queue immediately before this buffer. It is never shortened by host divergence. `inferredFrames` instead estimates unobserved loss and is capped to the host interval since the previous buffer (rounded half up). Use one loss type per step. Only uncovered host time is an `input` gap, padded before the writer loss. |
| `writerTail {id, untilMs, resumeMs?, writerFrames?, inferredFrames?}` | Requires a preceding `input`. With `writerFrames`, pad exactly that many admitted queue-dropped callback frames, even past the Stop trigger `untilMs`; without `resumeMs`, they immediately follow the last input. With `resumeMs`, uncovered host time ahead of the known frames is an input gap clipped to Stop. Without `writerFrames`, infer loss from the latest input's host end up to Stop. Optional `resumeMs`/`inferredFrames` locate an inferred interval immediately before a later buffer: only its overlap before Stop is padded. Uncovered input time ahead of that estimate is clipped/padded independently even if no writer loss overlaps Stop. Known and inferred counts are mutually exclusive; no missing audio is counted twice. |


Each recorded span is closed at the end of the case, as by a user Stop without a problem.
Offsets round half up. A writer that keeps an inert zero-frame span for a source declared while
deselected leaves it out of the comparison; such a span recorded nothing and receives no drops.
Prefix cases follow S-6: a reselected or boundary-declared span starts at the boundary.

Expected tracks hold `wire`, `startOffsetMs`, `frames` (including padding), `gapFrames`,
`droppedFrames` and `gaps`. In the expected tracks, `gapFrames: 0` and `droppedFrames: 0`
mean absent or zero. `atMs` and `durationMs` compare within 1e-6 ms; `frames` must be equal.
The overflow case gives `gapsCount`, `gapsHead` and `gapsTail` instead of 1001 literal
entries. `state` and `problemKind` are the saved session's; `problemKind: null` means absent.
`endReason` is the saved `end` event's reason: `user` when absent, `error` after host writer loss.
Where `expected.problem` is present, compare that string exactly. Writer loss contributes one
shared sentence per source, after any earlier problem, in source creation order. Sum writer
padding durations exactly before rounding milliseconds half up once for each source; producer
drops and input gaps are not writer loss. Cases without `expected.problem` do not constrain
ordinary gap/capture problem copy.
Native padding is appended on the sample timeline. Host-stamp divergence already
absorbed between inputs is not restored by tail inference and never shortens known loss.
The Mac D69.1 in-flight and tail repros use 80-frame buffers at 8 kHz: a callback at host
9 ms is dropped after the first buffer at 0; the following survivor or Stop is at 19 ms.
Known loss remains 80 frames (10 ms), whereas an inferred interval is capped to 72 (9 ms).

## presence.json

Stored track facts (`startOffsetMs`, `sampleRate`, `frames`, optional `gaps`) plus optional
ownership windows go in; integer half-open 16 kHz session-sample ranges `present` and
`unknown` come out. A track's extent starts at its D55 placement, the session sample
frame(startOffsetMs, 16000) (startOffsetMs × 16 rounded half away from zero), not at the raw
offset, and lasts frames/sampleRate from there; gap ranges and ownership stay on the session
timeline. A window [a,b) ms covers samples [ceil(16a - 1e-6), ceil(16b - 1e-6)).
A reader whose presence function takes an already placed offset runs these cases with
frame(startOffsetMs, 16000)/16 ms, as its transcriber passes it; the raw `startOffsetMs`
fails `a-fractional-start-offset-starts-at-the-d55-placement`.
A missing or null `gaps` is unknown. An empty gap (`durationMs` 0, `frames` 0) is no range.
Presence never reads PCM and never uses `droppedFrames`.

`tests/test_gap_ranges.py` checks the oracle against every literal case. It also checks
that presence from the raw offset, or from a half-even, floor or ceil placement, fails a
presence.json or writedown.json literal, and that coalescing ranges, truncating without an overflow marker, storing drops only on the live
span, flagging every gap, or deriving the flag from stored ranges alone each fail at least one
vector, as does counting host writer loss as a gap, keeping a user end after it, padding it on
top of the host time it covers, shortening known dropped callbacks to host time, allowing inferred loss beyond host/Stop time, or
padding a tail loss from the last written frame instead of where it lies, restoring absorbed
clock divergence, or losing uncovered input time when writer loss begins at/after Stop. Exact
problem literals reject the old disk-stall copy, half-even rounding, per-range sentences, and
dropping an earlier problem. Not covered here (app fault injection): a long gap together with
capture loss or a failed write, where io and capture take precedence.
