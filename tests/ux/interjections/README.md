# Transcript rendering vector: turns, interjections, "over", paragraphs

These are shared test vectors that every Cassini app (GNOME/Python, Mac and iOS/Swift, Android/Kotlin) checks its transcript renderer against.

**Rule 0: gocassini is the truth.** The gocassini web viewer defines how a transcript is shown. Each `*.expected.json` file was produced by running the viewer's own code on the matching `*.words.json` file. If an app renders differently, the app is wrong. If the viewer changes, regenerate the vectors; never edit them by hand.

**Rule 1: words are the atoms.** One item in `cassini.words.v1` is exactly one word with no whitespace. The viewer rejects any item that contains whitespace (`portable transcript item N must contain exactly one word`). A renderer places whole items and never splits, merges, re-punctuates or re-quotes their text. The viewer shows stored text as is: there is no quote pairing.

## Source

- Viewer: `codemyriad/gocassini`, commit `1111155cb0017b47c072a8662c54f28ad7c44223`. Each expected file records it in `source.commit`.
- Code paths: `cassini-viewer/src/viewer/portable.ts`, `src/viewer/loadArtifact.ts` (`buildPortableLoadedArtifact`), `src/viewer/meetingExport.ts` (`displaySegmentsForArtifact`), `src/core/overlap.ts` (`buildTranscriptRows`), `src/core/transcript.ts`, and `src/components/MeetingView.svelte` (continuation and crosstalk badge).

## Files

| Fixture | Pins |
|---|---|
| `acknowledgment` | A backchannel inside a turn becomes an inline chip; the host turn rejoins; there is no "over" |
| `competing` | Two speakers talking at the same time give two rows, each "over" the other |
| `monologue` | One speaker's paragraphs: grouping, no 100-item or 30 s split, continuation rows, timing repair |
| `overlapping-turns` | A turn's paragraphs stay together; "over" appears only on the first paragraph of a turn |
| `brief-reply` | No "over" under 150 ms; timing repair removes a fabricated overlap |
| `seam-gap` | A seam of 400 ms rejoins (chip); a seam of 450 ms does not (separate rows) |

`acknowledgment` and `competing` are the GNOME fixtures from cassini-gnome `tests/ux/interjections/` (as merged in #290, commit 722395a), with one correction. Ben's `"Sounds good."` was a single item, which the format forbids, so it is split into `Sounds` 3050–3300 and `good.` 3300–3550. An item holding several words gets its time shared equally between them. `competing` had no such item and is unchanged. The other four fixtures are synthetic. They use English text and the speakers `ada`, `ben` and `cy`. Items are sorted by `startMs`, with no ties.

## Regenerating

```sh
G=<gocassini clone>                 # at the commit you want to pin
(cd "$G/cassini-viewer" && npm ci)  # an npm workspace: installs into $G/node_modules
python3 -I make_fixtures.py         # only when changing fixtures; reads the two GNOME ones via git
GOCASSINI_COMMIT=$(git -C "$G" rev-parse HEAD) VIEWER="$G/cassini-viewer" \
  "$G/node_modules/.bin/jiti" generate.ts        # or: generate.ts some.words.json
```

`generate.ts` imports the viewer modules directly. It wraps the fixture as the `transcript` of a Cassini file, with an empty roster and no `provenance.wordTimings.endsBoundedByAudio`, so the timing repair runs. It then repeats the steps of `buildPortableLoadedArtifact`, calls `displaySegmentsForArtifact`, and calls `buildTranscriptRows`. Only two pieces of `MeetingView.svelte` are copied verbatim, because a `.svelte` file cannot be imported: `continuationRowKeys` with its 60 s gap, and the row crosstalk selector (`rowSpeechBlocks`). The script fails if any item is rendered twice or not at all, or if a block's text differs from its words.

## Schema (`cassini.render-vector.v1`)

```json
{
  "format": "cassini.render-vector.v1",
  "source": {"repo": "codemyriad/gocassini", "commit": "<sha>"},
  "fixture": "acknowledgment.words.json",
  "rows": [
    {"speaker": "ada", "continuation": false, "startMs": 0, "endMs": 8500,
     "over": [], "crosstalk": false,
     "members": [
       {"kind": "words", "wordIndices": [0,1,2,3,4,5,6], "text": "The release candidate is ready for testing,"},
       {"kind": "interjection", "speaker": "ben", "crosstalk": false, "wordIndices": [7,8], "text": "Sounds good."},
       {"kind": "words", "wordIndices": [9,10,11,12,13,14,15,16,17,18], "text": "and I will send ..."}]}
  ],
  "markdown": "# acknowledgment\n\n**ada** [0:00]\n\n..."
}
```

`rows` lists the transcript's rows (paragraphs) top to bottom.

| Field | Meaning |
|---|---|
| `speaker` | Speaker id of the turn. The viewer shows the speaker's label. |
| `continuation` | `true` hides the name badge, as the viewer's `continuationRowKeys` does. It is true when the row has the same speaker as the row directly above and `startMs − above.endMs ≤ 60000`. The timestamp is still shown. |
| `startMs` | The viewer's `row.startMs`, after timing repair. The row timestamp is `formatClockTime(startMs)`, and seeking starts here. |
| `endMs` | The viewer's `row.endMs`, after timing repair. It is used by the continuation rule on the next row. |
| `over` | The "(over A, B and C)" label: competing speakers as ids, largest overlap first, each named once. It appears only on the first paragraph of a turn. Interjection pairs never appear here. The display string uses `", "` and a final `" and "`. |
| `crosstalk` | The "probably crosstalk" badge. It is true when every canonical word of the row's own speech blocks (chips excluded) has `lowConfidenceSpeaker: true`. |
| `members` | What the row's paragraph shows, in order, joined by single spaces. |
| `members[].kind` | `"words"` is the turn speaker's own speech (one viewer block). `"interjection"` is an inline chip. |
| `members[].wordIndices` | Indices into the fixture's `items`, in display order. Every item appears exactly once in the whole file. |
| `members[].speaker` | Chips only: the speaker id shown in the chip. Its copy text is `(Label: words)`. |
| `members[].crosstalk` | Chips only: the warning border. Same rule as the row flag, applied to the chip's own words. |
| `members[].text` | Informative. The items' text joined by single spaces. |
| `markdown` | Informative. The viewer's own `transcriptMarkdown` (Copy/Download) output, with labels equal to the ids. |

**Comparing.** Normative fields are `speaker`, `continuation`, `startMs`, `endMs`, `over` (order included), `crosstalk`, and `kind`/`speaker`/`crosstalk`/`wordIndices` of members. Two adjacent `"words"` members in one row render identically to one member, because they are joined by a space. Their boundary is the viewer's block seam and is informative. An app that does not model blocks may concatenate adjacent `"words"` members before comparing. None of these fixtures sets `lowConfidenceSpeaker`, so every `crosstalk` here is `false`.

## Rules the vectors pin (viewer behaviour at 1111155)

1. **Paragraph blocks for a Cassini file** come from `groupTranscriptSegmentsAsReadable` plus `mergeSmallReadableSegments` (portable.ts), applied to the items in file order:
   - Start a new block at any speaker change between consecutive items, at a gap over 4200 ms, past 90 s or 140 words, or once a block has reached 96 words or 45 s and ends a sentence (`[.!?]` plus closing quotes) or is followed by a gap of 2200 ms or more.
   - Then merge consecutive same-speaker blocks when either side has fewer than 18 words or lasts under 8 s, provided the gap is ≤ 4200 ms and the merged block stays within the limits.
   - The 1500 ms producer pause rule belongs to the Go producer's own segments. It does not apply here.
2. **Timing repair runs first** (`repairTurnFinalWordInflation`). A word ending in `.?!,;:…` (closing quotes allowed) that runs longer than `max(1000 ms, 4 × median)` is clipped to that length. The median is the speaker's own when they have ≥ 8 timed words, else the meeting's, else 250 ms. The repaired ends feed everything downstream: rows, overlap and continuation. It is skipped only when the file says `provenance.wordTimings.endsBoundedByAudio: true`.
3. **Rejoin.** A speaker's next block joins their open run when `|block start − run end| ≤ 400 ms` and the seam has continuity evidence. Otherwise it starts a new turn.
4. **Interjection.** A run becomes a chip when all of these hold:
   - its audible time is ≤ 2000 ms;
   - another speaker's single run was split around it, with side gaps ≤ 500 ms;
   - it does not end after the host run ends (`run.endMs <= host.endMs`, overlap.ts:1441);
   - at most one host block starts inside it;
   - nesting is one level only.

   The chip sits right after the host block that precedes it. A chip is never a row and never produces "over".
5. **Over.** Two runs of different speakers whose audible spans intersect by ≥ 150 ms, and neither nested in the other, are "competing". Each turn names the other in `over`, on its first paragraph only.
6. **Paragraphs of a turn** split only between two of the turn's own speech blocks that are adjacent in reading order, never right after a chip. There is no item-count or duration limit. A turn's paragraphs stay together, and turns appear in the order they started. Paragraphs are never interleaved by start time.
7. **Continuation and crosstalk** follow the field rules above.
