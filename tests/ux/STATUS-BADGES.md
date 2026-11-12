# Status badges — W0.4 / L-25, L-26

This amends HARMONY sections 2–3. The source is the alignment canvas
`ui/Spec-Badge.dc.html`, `ui/Badge-*.dc.html` and decisions L-25/L-26,
summarised in `vision-canvas/spec/components-a.md` section 2. Section 10's
colour rules are a separate change; this document does not replace them.

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
| received | received | Received | outline | incoming |

Tone roles: red uses `record-tint` / `record-ink`; neutral uses `neutral-tint`
/ `ink-2`; amber uses `warn-tint` / `warn-ink`; outline is transparent, with
`line` outline and `ink-2` text. These are token references, not another palette.
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
| Phone-specific states / Received eligibility (L-10) | Existing per-app applicability retained. Current iOS labels include Queued, Paused at {clock}, Transcribed, Audio only, Newer format, Can’t play, Can’t read transcript; none is newly mapped here (IOS-COM-04) |
| Search-result placement | Existing placement retained; the Badge purpose mentions search, but result-card drawings omit it |
| Position/detail placement | Existing meeting/detail placement retained; exact placement is not drawn |
| Selected-row override | No new override rule. Current Mac app uses selected-white badge ink; identical selected/rest badge styling is drawn but marked proposed in Spec-Badge. Existing per-app behavior is retained until adoption |
| Exact metrics | Current drawn padding 0/8, gap4, dot6, spinner10, alert/incoming11, outline1; Mac height18/type11.5, GNOME22/13, phones22/12; native font families as drawn. Use named constants, not a new universal platform size |

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
