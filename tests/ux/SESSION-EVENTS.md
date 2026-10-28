# S-6: what is recorded, and fallback, in session.json (coordinator draft, 2026-10-28)

Status: **binding for wave 6** in both apps. Mac owns folding it into cassini-browser-protocol (`SPEC.md` and
`schemas/session.schema.json`) after its W5. GNOME reviews that. Until then this file is the contract.

## No version bump

session.json stays at `version: 1`. Schema v1 already has:
- per-track `startOffsetMs` / `endOffsetMs` / `ended`, so a track can start or end mid-meeting;
- `additionalProperties: true` at the top level.

S-6 only adds an optional top-level `events` array. Existing readers ignore it. Vendored v1.2 protocol files need no
change to write it.

## Who a track belongs to

Derived from `kind`. No new field.

| kind | role (WhatIsRecorded) |
|---|---|
| `microphone`, `local` | **Me** |
| `remote`, `tab`, `media`, `system`, `application` | **The others** |

- Every capture gets its own track and its own WAV. Never append audio from a different source to an existing
  track.
- A track that starts mid-meeting has its `startOffsetMs` on the session timeline. It is finalised with
  `ended: true` and its `endOffsetMs` when that capture stops.
- **`sourceTrackId` (optional, per track; added 2026-10-28 at GNOME's request).**
  - The opaque ID of the capture source a track came from: the browser remote track ID, for example.
  - When "The others" is turned off and on again while the same browser track is still connected, the new track
    gets a new unique `id` and WAV, but carries the same `sourceTrackId`.
  - Writing down (D11) treats all tracks that share a `sourceTrackId` as **one speaker**, so a person doesn't
    become a new "Speaker n" after every off/on.
  - Never derive identity from labels. Tracks without it are separate sources, as today.
  - Opaque string, at most 200 UTF-8 bytes. Writers omit it for microphone and system tracks, which keep their
    Me / The others identity anyway.
- D17: each browser meeting is its own folder with its own session.json, its own `events`, and its own copy of the
  microphone track.

## `events`

```json
"events": [
  {"atMs": 0,      "type": "othersSource", "source": "browser", "reason": "start"},
  {"atMs": 0,      "type": "selection",    "me": true, "others": true, "reason": "start"},
  {"atMs": 754210, "type": "othersSource", "source": "system",  "reason": "lost"},
  {"atMs": 900000, "type": "selection",    "me": true, "others": false, "reason": "user"}
]
```

Common rules:
- `atMs` is a number in milliseconds on the same timeline as `startOffsetMs`, with `0` = session start. Ordered,
  non-decreasing, append-only, at most 1000 entries.
- Unknown `type` values and unknown fields must be ignored by readers. That leaves room for later events.
- Durability:
  - Write the event in the same checkpoint as the track change it explains: before the new track is acknowledged
    or used.
  - If no track changes (e.g. the others are switched off), write it immediately.
  - Use the existing sync-then-rename rule.

### `type: "othersSource"`

How "the others" are being recorded from this moment on.
- `source`:
  - `"browser"`: the companion's per-person or tab tracks;
  - `"system"`: this computer's sound;
  - `"none"`: the others are not recorded, either because the person chose that or because no path exists.
- `reason`:
  - `"start"`;
  - `"missing"`: the browser path was expected but absent at start;
  - `"lost"`: the tab reloaded, the browser closed, the extension crashed, or the host disconnected;
  - `"user"`.

  `"restored"` is reserved; see below.
- Q4 fallback is exactly `{"type": "othersSource", "source": "system", "reason": "missing" | "lost"}`, plus a new
  `system` track starting at that `atMs`.
- `errors.othersFallback.line` with "Why?" is shown whenever the newest `othersSource` event has reason `missing`
  or `lost`.
- Never fall back to the microphone.
- **No automatic switch back in wave 6.** Once on `system`, a meeting stays on `system` until Stop, even if the
  companion reconnects. This avoids flapping and duplicate audio. `reason: "restored"` is reserved for a later
  decision.

### `type: "selection"`

The WhatIsRecorded choice (Me / The others / Both).
- `me` and `others` are booleans. At least one must be true; the dialog never allows neither.
- `reason`: `"start"` or `"user"`.
- Turning a side off finalises its live tracks at `atMs`. Turning it on starts new tracks at `atMs`. When the
  others are turned off, add an `othersSource` event with `source: "none"` and `reason: "user"`.

## Readers

- **Library, Recording page, Export:**
  - Sources and badges come from tracks, as today.
  - `events` adds the info line (fallback) and the "since" text where boards draw it.
  - A folder without `events` is a v1 folder: no fallback line, and nothing is inferred.
- **Export (Cassini file):**
  - Mix tracks by offsets, as today. D19 rules apply when offsets are unknown.
  - `events` are not exported in wave 6.
- **Recovery:** events after the last fully synced checkpoint may be lost. Readers must not infer events from track
  gaps.

## Tests (both apps)

- A shared fixture under `ux-draft/session-events/`: a browser start, a `lost` at 754210 ms with a system track
  from there, then Me-only from 900000 ms.
  - The first app to implement it writes and commits the fixture. The other app adds a reader test against it.
- Writers: a crash between the new track and the event checkpoint never leaves an event without its track, or a
  track without its event.
