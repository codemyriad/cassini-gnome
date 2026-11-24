# GNOME parity audit, 2026-11-11

Reference: cassini-ux `b86b7b0ecfa0984e313380578863f2f75fd652f1` `PARITY.md` and `decisions/DECISIONS-UX.md`.
The parity table dates from 6 November. GNOME code was checked at `194f8e7`,
including playback speed, filters, Opus import and system-language support.
The complete missing-row audit and product questions are in shared
`STATUS-GNOME.md` in the coordination checkout on drmeyers.

## Selected: search meeting titles

D35, medium item 7, approves title/speaker search. GNOME's Search page indexed
only spoken words; renames deliberately avoided rebuilding those rows. Both
mobile apps already search titles, including notes without a transcript:

- [Android title-only, rename and audio-only tests](https://github.com/codemyriad/cassini-android/blob/c46dbb3bb4d18f2963c3debddd30899d1a097b17/data/src/test/kotlin/io/codemyriad/cassini/data/db/NoteSearchTest.kt).
- [iOS matching title-only, rename and audio-only tests](https://github.com/codemyriad/cassini-ios/blob/7ac55e88bb1c1baad6c3d261a7889dc0f8fffb7f/Packages/CassiniNotebook/Tests/CassiniNotebookTests/Library/NoteSearchTests.swift).

A separate FTS title index adds title matches using the mobile term/token rules:
whitespace-separated prefix phrases match independently with AND. Punctuation
inside one term splits adjacent tokens, with prefix completion on its last token.
A title-only result has no timed word or speaker and starts playback at zero.
When words also match, their result positions are retained and the title is
highlighted without adding another result. Date and ownership filters restrict
title results. D14's speaker filter remains turn-scoped, so title-only results
are excluded when a speaker is selected; a title never inherits a participant's
attribution. Renames update the title index without rebuilding
spoken rows; old transcript-only caches gain the additive title table on sync.
No meeting files or shared contract files change.

Validation: 94 Search unit cases, including multi-term prefixes, nonadjacent
terms, case/accent folding in all six supported locales, literal operators,
Unicode whitespace, combining marks, cache upgrade, rename invalidation, filter
scope, escaping, audio-only results and unchanged spoken-word seeks. The real
Search Board exercises metadata rename, displayed title markup, removal of the
transcript reference, playback and Open Meeting. Omitting production title
matches fails that Board; retaining stale title entries fails the rename test.
A 100,000-word (11-hour) case rejects any transcript-row rebuild during a title rename.
No fixed sleeps, timing pass budgets or renderer production changes are added.

## Remaining gaps and decisions

- Ordinary audio import: shipped on Android/iOS and approved by D35 item 11;
  desktop's byte-preserved Opus received-file contract still needs a raw-source
  retention/conversion rule (Chris/Silvio).
- Recording pause/resume and acoustic-checkpoint transcription resume: mobile
  behaviors; desktop source/timeline and persistence policy is not recorded.
- System/lock-screen playback: Android/iOS ship it. D53 is mobile-scoped;
  desktop MPRIS scope needs a product decision. GNOME has window media keys.
- Tags: Nextcloud only; no desktop contract. Summary/insights: Nextcloud only,
  HARMONY §11 remains undrawn. Neither meets the two-shipping-app rule.
- Original/uncompressed audio export: iOS has original audio; D35 explicitly
  leaves uncompressed WAV format/storage to Silvio.

Intentional differences remain: mobile “note” versus desktop “meeting”;
iOS-only bookmarks/stars/passage edits (D53.1); desktop's exactly two export
formats; system-selected GNOME language (D50); paused Flathub publication
(D45.4). These are not silently converted into implementation requirements.
