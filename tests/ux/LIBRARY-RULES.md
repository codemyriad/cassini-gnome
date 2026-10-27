# Library derivation rules (D6): binding for both apps

Owner: the coordinator. These rules close every gap the wave-1 reviews found: 38 confirmed findings on GNOME #6 and
34 on Mac #3, many of them places where the two scanners would show the same folder differently. LIBRARY.md
describes the files; **this document says how a meeting row is derived from them**.

Where LIBRARY.md and this document differ, this one wins. Each rule is tested by `ux-draft/library-vectors/`
(S-3a): small Library trees, each with an `expected.json` that both apps must reproduce exactly.

## R1. What is a meeting

1. **Folder names.** A meeting folder is a direct child of the root named `Recording-<uuid>` or `Received-<uuid>`.
   `<uuid>` must be the canonical lowercase form, 8-4-4-4-12 lowercase hex with hyphens. Any other name is not a
   meeting: braces, `urn:uuid:`, uppercase, missing hyphens. Ignore it silently.
2. **Symlinks.**
   - A meeting folder that is a symlink is not a meeting. Never follow it.
   - Inside a meeting folder, only regular files count. A symlink, FIFO, socket or device named like a meeting file is
     treated as unreadable. Check with `lstat`/`O_NOFOLLOW` before opening, and never block on it.
3. **The Library root.**
   - The root may itself be a symlink; resolve it once.
   - If the root does not exist and is not a symlink, the Library is empty (first run).
   - If the root is a dangling symlink, or exists but cannot be listed, the scan failed. That is the future
     `library-offline` state, never "empty".
4. **Received folders.** A `Received-<uuid>` folder without a valid `meeting.json` (one that passes R2) is an import
   in progress or an abandoned import. **It is not listed.**
5. **Recording folders.** A `Recording-<uuid>` folder is always listed, whatever is unreadable inside it (D3).

## R2. Reading meeting.json

1. **All or nothing.** Parse the file and validate it against `meeting.schema.json`.
   - If it does not parse, or is invalid for its version, ignore **the whole file**: treat it as absent and record a
     diagnostic.
   - Never salvage individual fields.
   - Never rewrite the file automatically.
2. **Newer versions.** If `version` is greater than the version the app supports, the app data is ignored for display
   (as if absent) and the meeting is read-only for metadata edits. This is diagnostic `unsupported-version`, not
   `invalid`.
3. **meeting.json absent or ignored**, the fallbacks:
   - For a Recording: a regular `transcript.json` in the folder counts as a sidecar reference.
   - For a Received meeting: R1.4 applies, so it is not listed at all.

## R3. Transcript status, ETA and error (precedence)

The stored `transcript.status` is the starting point. Then apply the first matching rule:

| Stored status | Body (sidecar / embedded) | Result |
|---|---|---|
| queued / running | any, including missing or unreadable | Keep the status. Missing or unreadable means "no words yet", not damaged. Keep `etaMinutes`. If it is null or invalid, the badge is **"Transcribing…"** (catalog `states.badge.transcribing.noEta`). |
| failed | any | Keep `failed`. The error is the stored error. If that is null, the error is `{transcribe-failed, "Try again"}`. |
| done | readable | `done` |
| done | missing / unreadable / mismatched | `damaged`. The error is the stored error if it has an action; otherwise `{transcript-damaged, null}`. |
| damaged | any | `damaged`, with the same error rule as the previous row |
| none | any | `none`. A body never promotes `none` to `done`. |
| *(meeting.json absent or ignored)* | `transcript.json` readable / unreadable / absent | `done` / `damaged` with `{transcript-damaged, null}` / `none` |

Rules for references:
- A reference that is not null and not a valid object makes the whole meeting.json invalid (R2.1).
- An embedded reference is readable only if all of these hold:
  - the folder is a `Received-` folder
  - `reference.file == received.originalName`
  - the file opens with the cassini-format reader
  - the named `transcriptId` exists. When `transcriptId` is omitted, the file's default transcript is used. When
    `transcriptId` is given but missing, the body is **unreadable** (never fall back to the default).
- A received file whose audio digest does not match its manifest, or whose portable format is unknown: the transcript
  body is unreadable (→ damaged per the table). The audio still plays, and the duration still comes from the Opus
  stream (R5).
- **Sidecar parsing is strict.** Any item missing `speaker`, `startMs`, `endMs` or `text`, or with an empty speaker id,
  `startMs > endMs`, or negative times, makes the whole body unreadable. **Embedded** transcripts follow the
  cassini-format reader exactly; do not add your own clamping.

The stored error wins over a derived error whenever it has an action.

## R4. lifecycle and interruptedAtMs

1. `lifecycle` comes from the capture-activity source (D2). If no live report exists, it is `saved`.
2. `interruptedAtMs` is set **only** for:
   - (a) session `state: "interrupted"`
   - (b) `state: "recording"` with no live report (D2)
   - (c) an unreadable session.json, or one with an unsupported `version` or a mismatched `id`. This is the D3
     salvage path.
   - (d) `gapFrames > 0` on any track, or `problemKind: "gap"`

   Its value is the **display end** (R5).
3. **Per-track problems never set `interruptedAtMs`.** A missing, unreadable or mismatched WAV (rate, channels,
   header), a track with `ended: false` inside a `completed` session, or an out-of-range `startOffsetMs` only add a
   diagnostic. A `completed` session is trusted.

## R5. Duration and display end

1. **A track counts** if it is listed in session.json (or found as `track-*.wav` on the D3 salvage path) and its WAV
   is readable. Complete frames are `floor(dataBytes / blockAlign)`, ignoring a partial trailing frame.
2. Track end = `(startOffsetMs, or 0 if unknown or out of range) + floor(frames * 1000 / rate)`.
   Duration = max over counted tracks.
3. If no track counts, the **duration is unknown**. It is not 0 and not "under 1 min".
4. **Received:** duration = Opus sample count (last granule position minus pre-skip) / 48000, floored to ms. If the
   stream cannot be read, the duration is unknown.
5. **Display of an unknown duration:**
   - Row meta: drop the duration and show only the names. If there are no names either, the meta line is not shown.
   - Preview subtitle: `{day}, {time}` with no duration.
   - `interruptedAtMs` is null when the display end is unknown, even if R4 would set it, so no "Interrupted at" badge
     is shown. This applies only when no track counts at all. D3's display-only zero offset still gives a known end
     whenever a readable WAV exists.

## R6. Title, start, order, groups

1. **Title.** Use the first non-empty value (after trimming whitespace) from:
   - meeting.json `title`
   - session.json `title`
   - the embedded manifest title
   - the Vorbis `TITLE` tag
   - `received.originalName` without its extension

   Otherwise use **"Untitled meeting"** (catalog `library.untitled`).
2. **Start.** Use, in order: session.json `createdAt`, embedded `createdAtUtc`, `received.importedAtMs`. Otherwise
   the start is unknown. Never use file mtimes.
3. **Order:**
   - Meetings with a known start: by start, newest first. Ties are broken by uuid, ascending.
   - Groups: day groups per format-vectors `dayGroup`, newest day first.
   - Meetings with an unknown start go in a last group, **"Date unknown"** (catalog `library.group.unknownDate`),
     ordered by title (case-insensitive, then exact), then uuid.
4. **Activity ids.** Capture-activity ids are compared in canonical lowercase.

## R7. Speakers, names, numbering

1. **Speaking speakers** are the speaker ids that have at least one transcript item. They are ordered by the
   `startMs` of their first item (ties: file order). Their 0-based position is `colourIndex`.
2. **Name** of a speaking speaker, using the first that applies:
   - meeting.json `speakers[id].name`, if non-null
   - for received meetings, the embedded speaker label, if it is not of the form `Speaker <n>`
   - otherwise **"Speaker {position+1}"**, where the position is in the speaking order of rule 1

   The stored `colourIndex` is ignored for numbering.
3. **Row meta names** list only speaking speakers, in speaking order, with at most 3 names and then `+{k}`
   (format-vectors `rowMeta`). Speakers stored in meeting.json who never speak are not listed.

## R8. Quote

1. **A turn** is a maximal run of **consecutive** transcript items, in `startMs` order (ties: file order), with the
   same speaker. There is no seam-joining across interjections, and there are no aside rules.
   - Turn duration = last item's `endMs` minus the first item's `startMs`.
2. **Candidates** are turns whose first item starts at `startMs < 600000`. Pick the longest. Ties go to the earliest
   start, then file order.
3. **Text.**
   - Trim each item's text and drop empty ones.
   - Join the items with one space, then collapse every run of whitespace to one space.
   - Do not convert characters: keep straight quotes inside the text.
   - Wrap the result in “ ”.
4. **The override (`quoteOverride`)** is trimmed and whitespace-collapsed. One pair of outer quotes, straight or
   typographic, is stripped. Then it is wrapped in “ ”. An override that is empty after this is ignored.
5. **A quote is shown only when words are available.** That means `transcript` is `done`, or `queued`/`running` with at
   least one readable item. Otherwise there is **no quote**, not even the override.

## R9. canWriteDown

`canWriteDown` is true when:
- lifecycle is `saved`, and
- transcript is `none` or `failed`, and
- the meeting has at least one counted track (own) or a readable Opus stream (received).

A received meeting whose transcript is `none` can be written down once (HARMONY 3).

## R10. Watching and performance (both apps, same behaviour)

1. **Events that trigger a rescan:**
   - Creation, deletion or rename of a meeting folder.
   - A change to `session.json`, `meeting.json`, `transcript.json` or the received `.opus`.

   WAV growth during a recording does **not** trigger a rescan: the live duration comes from the capture-activity
   source.
2. **Rescans are incremental.** Rescan only the affected meeting folder. Do a full rescan only on events at the root.
3. **Debounce.** Use a trailing-edge debounce of 250 ms, with a maximum wait of 2 s.
4. **Scans run off the UI thread.** Deliver results to the UI as a complete snapshot.
5. **Cache parsed files** per meeting file, keyed by `(device, inode, size, mtime_ns)`. Never re-hash or re-parse an
   unchanged received `.opus` or `transcript.json`.

## New catalog entries (D6, coordinator normalisations of undrawn states)

- `library.untitled` = "Untitled meeting"
- `library.group.unknownDate` = "Date unknown"
- `states.badge.transcribing.noEta` = "Transcribing…" (grey/accent as the transcribing badge)

## D6.1 amendments (answers to GNOME R4–R7 and Mac's ETA question), 2026-10-27

- **ETA (R2 vs R3).** `meeting.schema.json` now allows `etaMinutes: null` while the transcript is queued/running.
  No reader exception is needed: null means "Transcribing…". A non-integer ETA is schema-invalid, so R2 applies
  (the whole file is ignored).
- **Fractional offsets (R5.2).** Durations and `interruptedAtMs` are integers. Track end =
  `floor((startOffsetMs or 0) + frames * 1000 / rate)`, computed exactly (rationals or integer arithmetic, no binary
  float drift). The duration is the max of these. Example: offset 12.75 and 48 frames at 48000 Hz gives `floor(13.75)`
  = 13.
- **The same UUID under both prefixes (R1).** `Recording-U` and `Received-U` are both listed as separate meetings.
  Each keeps its UUID as `id`. Apps key them internally by folder basename. Final sort tie-break: folder basename,
  ascending (this is GNOME's proposal).
- **Received metadata (R1.4).** A `Received-` folder needs a valid meeting.json **with a non-null `received`
  object**, otherwise it is not listed. A `Recording-` meeting.json with a non-null `received` is invalid (R2: ignore
  the whole file).

## R11. Row texts by state (D9, from the wave-2 screenshot comparison), 2026-10-27

A Library row has a title line (title + time), a **meta** line that sits after the badge, and a **second line**.

| Badge state | Meta (after the badge) | Second line |
|---|---|---|
| recording, browser capture (session source `chromium` or any browser host) | `common.listeningInsideBrowser` | `library.row.recording.line` |
| recording, other capture | *(none, until the variants exist)* | *(none)* |
| saving | *(none)* | *(none)* |
| transcribing | *(none)* | `library.row.transcribing.line` ("{duration} · the audio is ready to play"); none if the duration is unknown |
| everything else (needs attention, interrupted, received, no badge) | `{duration} · {names}` per format-vectors `rowMeta` and R5/R7 | the quote (R8), or none |

**Sidebar header:**
- While any meeting has `lifecycle = recording`, the toolbar Record button is replaced by the **live pill**: dot, live
  timer, two level bars, Stop.
- Before UX5 it may be a static rendering, but both apps show it for the same fixture.

**Preview transcript excerpt:** speaker names are in the normal text colour, with no speaker colours. Speaker colours
belong to the Meeting view (UX3).
