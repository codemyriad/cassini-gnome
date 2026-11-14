# Status badges — W0.4 / L-25, L-26, L-45, L-46, L-52, L-54

This amends HARMONY sections 2–3. The source is the alignment canvas
`ui/Spec-Badge.dc.html`, `ui/Badge-*.dc.html` and decisions L-25, L-26, L-45,
L-46, L-52 and L-54. Section 10's colour rules live in HARMONY.md; this document
complements them.

## Decided

One derived badge per meeting; pill shape, weight 600, tabular digits and a
glyph on every badge. Labels are catalog strings, never app-local shortened
copy. Detailed stopping position and detailed remaining time belong in the
meeting, outside the badge.

| Model kind | Catalog id (`states.badge.` prefix) | English example | Tone | Glyph |
|---|---|---|---|---|
| recording | recording | Recording 12:48 | red | dot |
| importing (transient entry) | importing | Importing… | neutral | spinner |
| saving | saving | Saving… | neutral | spinner |
| transcribing (positive ETA) | transcribing | Transcribing · 4 min | neutral | spinner |
| needsAttention | needsAttention | Needs attention | amber | alert |
| interrupted | interrupted | Interrupted | amber | alert |
| received | received | Imported | outline | incoming |

Tone roles: red uses `record-tint` / `record-ink`; neutral uses `ink` at 10%
opacity for its fill and opaque `ink-2` for text/glyphs (**exception, L-45:**
on all four apps, the `transcribing` badge's spinner glyph uses `accent` so it
matches the open meeting's status line); amber uses `warn-tint` / `warn-ink`;
error (phones, L-52/L-54) uses `error-tint` / `error`; outline is transparent,
with `1px` `line` outline and `ink-2` text. These are token references, not
another palette.

**Exact metrics (decided, L-54):** badges are built the same on every platform —
`22px` tall, `12px` weight 600 text (`line-height: 1`, tabular digits), `8px`
horizontal padding (`0 8px`), `4px` gap between glyph and text, `6px` dot,
`10px` spinner / pause, `11px` alert / incoming / clock / wave, `1px` `line`
outline. This replaces the earlier per-platform sizes (Mac `18/11.5`, GNOME
`22/13`).

### Phone badge and row-pill set (decided, L-52, L-54)

Row badges follow each app's own states: the look and `22/12/8` metrics are
shared, the set is not. Desktop keeps the six persisted kinds above plus
transient `importing`. Phones (`cassini-ios`, `cassini-android`) carry no
status chip on a finished meeting (`Transcribed` is dropped visually; screen
readers still hear `transcribed`, catalog `phone.row.transcribed.a11y`). Instead
finished phone rows show the speaker count as a neutral pill (`ink` at 10%,
people icon + `phone.row.speakerCount` from `1 speaker` upward) or `No speech`
(`phone.row.noSpeech`), plus on iOS the bookmark count as a neutral pill
(bookmark icon + number only), with no middle dots on the line.

Every unfinished or problem phone row state keeps its `22/12/8` badge:

| Phone state | Catalog id | English | Tone | Glyph | Row action |
|---|---|---|---|---|---|
| recording | `states.badge.recording` | `Recording 12:48` | red | dot | — |
| importing | `states.badge.importing` | `Importing…` | neutral | spinner | — |
| transcribing | `states.badge.transcribing` | `Transcribing · 4 min` | neutral | spinner (`accent`) | — |
| queued | `states.phone.queued` | `Queued` | neutral | clock | — |
| paused | `states.phone.pausedAt` | `Paused at {position}` | amber | pause | `Resume` (`phone.row.resume`, `ink` semibold) |
| failed | `states.phone.couldntTranscribe` | `Couldn’t transcribe` | error (`error-tint` / `error`) | alert | `Try again` (`common.tryAgain`, `ink` semibold) |
| audioOnly | `states.phone.audioOnly` | `Audio only` | neutral | wave | — |
| unplayable | `states.phone.cantPlay` | `Can’t play` | error (`error-tint` / `error`) | alert | — |
| unreadable | `states.phone.cantReadTranscript` | `Can’t read transcript` | error (`error-tint` / `error`) | alert | — |
| newerFormat | `states.phone.newerFormat` | `Newer format` | outline | alert | — |

> **WP7 note (L-57):** the user-facing rename of `Received` to `Imported` has
> landed on desktop (macOS & GNOME) in `WP7` (`cassini-ux` #19). Only the
> English copy changed: the catalog id stays `states.badge.received` and the
> model kind stays `origin = received`. `states-vectors.json` v3 is unchanged.
>
> **L-10 (`D-997`, `D99`):** phones adopt it. A finished imported phone row
> shows the outline `Imported` badge (`states.badge.received`, incoming glyph)
> before the speaker-count pill; any badge from the table above replaces it
> while that state applies. See `PARITY.md`, Phase 2 parity contract §1.

L-46 supersedes the earlier neutral-tint fill: composite the neutral wash over
whatever is behind the pill, including selected surfaces, in both light and
dark appearances. Do not substitute an opaque `neutral-tint` or fade the whole
badge; text and glyph opacity are unchanged. Source: Alex's second token reply
(12 Nov), summarised in `vision-canvas/lessons/alex-tokens-response-2.md`,
“For every app”, item 5. This is a fill treatment, not a new tone id, label,
state, precedence or selected-row foreground override.
The canvas visual name `attention` maps to the current model id
`needsAttention`; identifiers are not new user-facing copy.

The incoming glyph is the full arrow into a tray, not an upload arrow.
Record uses the existing live timer, including hours from one hour onward.
Positive integer ETA uses the compact label, including `Transcribing · 1 min`.
`meeting.interruptedAt` preserves the current position-bearing sentence for
meeting/detail consumers; it must not be used as badge text.

## Scope and provisional current values — pending Alex

These are explicit gaps, not new design decisions. Keep their current values
until the canvas resolves them:

| Question | Current value retained, provisional pending Alex |
|---|---|
| Under one minute | `Transcribing, less than a minute left`, with current translations (`states.badge.transcribing.underMinute`) |
| No valid ETA | `Transcribing…`, with current translations (`states.badge.transcribing.noEta`); D6 R3 fallback remains |
| Persisted state and priority | lifecycle remains recording/saving/saved; existing meeting precedence remains recording → saving → transcribing → needsAttention → interrupted → received → none |
| Importing against an existing meeting | No shared cross-domain priority or persisted importing lifecycle currently exists; none is invented here |
| Import cancellation/failure | No additional badge label or new transition rule specified here; existing app behavior remains |
| Phone Imported eligibility (L-10) | Phones keep their existing import flow until L-10 is decided; desktop `Received` → `Imported` has landed in WP7 (L-57) |
| Search-result placement | Existing placement retained; the Badge purpose mentions search, but result-card drawings omit it |
| Position/detail placement | Transcribing status line + sheet/popover are decided by L-45; interrupted stopping position stays in `meeting.interruptedAt` outside the badge |
| Selected-row override | No new override rule. Existing per-app behavior is retained until Alex resolves selected-row badge styling |

Importing is decided for an active import-entry card (L-25/L-26). It is tested
in `entryVectors` separately from persisted meeting `vectors`: the fixture
input `{importing: true}` denotes that active entry, not an added metadata
field or a rule that importing beats recording. This technical separation
lets every app test the decided visual kind while the unanswered priority
and persistence questions stay unanswered. Consumers must exercise both arrays.

Persisted-file validation is unchanged: schema-invalid ETA still follows
LIBRARY-RULES R2/D6.1 (including rejecting the affected metadata). The
no-ETA label does not license accepting schema-invalid files.

The new Importing translations use the glossary's existing language/style;
they and compact localized ETA labels remain subject to the normal language
review. No alternative unapproved English label is introduced.

## Pin and adoption

Vendor the catalog whole and vectors at **one merged commit**, with SHA-256
receipts. State vectors v3 replace v2; no `accent`/`grey` tone aliases or legacy
`dot` boolean are retained in badge output. Every non-null badge has exactly
`kind`, `text`, `tint`, `glyph`. An empty badge remains null. The existing
`canWriteDown` policy is unchanged.

Also vendor format vectors v5 and the changed Library expected rows at that
commit. Only their badge presentation changes; audio, transcript, identity,
sorting, row anatomy and error-action behavior do not. Run
`python3 vectors/states/verify.py` to cross-check labels, translations,
glyphs/tones, precedence, ETA fallbacks and related expected text.
