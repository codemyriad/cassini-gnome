# D52 source diagnostics — D52.1 / R65

Cassini preserves an optional `recordingDetails.captureRecord` in meeting.json
before finalization. The existing S-12 mapping carries it verbatim inside
`x["codemyriad.io"].meeting.recordingDetails`. No library_v2 core or shared schema
has been edited. D52.1 approves this shared placement and source-report copy; the coordinator
owns the matching schema update.

The record is version 1. `sources` copies the protocol's complete captureOutcome
source rows (including never-opened/refused sources); `tracks` retains id, kind,
label, sourceTrackId, frames, sampleRate, channels, startOffsetMs, endOffsetMs,
gapFrames, droppedFrames, problemKind, captureOutcome and receivedFrames when
present. `events` copies S-6 capture events, including the terminal reason.
Optional problemKind retains the capture-wide loss classification. No voiceprint,
match vector, device identity, source WAV path or source URL is included.

Absence means unknown. Zero/DC PCM remains received audio. Padding does not
increase audioMs. No source list is truncated to the WAV limit. Future fields
survive repeated writes; a newer record version is preserved without downgrading.
The report reader rejects malformed known fields without inventing success.

The owner writes after closing the capture and before publication, outside the
PCM locks, under the metadata transaction's process lock and stable flock.
Startup recovery uses the same path. A failed metadata write prevents publication
and keeps the completed PCM available for retry. Repeated identical writes do
not advance metadata revision. Cache-free file reading must retain the report;
the regression publishes through the real encoder/journal, deletes the session
cache and checks the file-derived record and unchanged original WAV bytes.

The Recording page warns only for a confirmed failure of an expected source.
Startup neverAudio/lowAudio and intentionally unselected sources do not warn.
Recording details and Copy details list every announced source, its outcome and
separate gap/drop facts, using the approved catalog and meetingDuration formatter.
The six-language entry objects and D52.1 decision are pinned in tests/ux/d52/.
Language selection remains the separate D50 item.

Recording details uses the embedded record when the stopped session cache is
absent. Its original capture counters, source spans and end event remain facts
about the capture even when the playable file has compacted tracks. This does
not recreate cache folders or open paths from the report. Known invalid track
facts are ignored; unknown offsets remain unknown and no levels are inferred.
The display keeps the existing shared catalog and gap formatter.

R69 adds optional source-row `captureExpected: false`: the browser source was
never selected for admission. Its factual outcome and frame counts remain,
but it does not flag an otherwise healthy recording. Missing means true. The
owner derives this from selection/ownership, never from a client-supplied bit;
a later selection or recorded span permanently makes the source expected.
The portable reader validates the bit without inventing it from timing or
labels. Replacing known facts clears an obsolete false bit while preserving
future fields. Source facts keep unused sources neutral after cache deletion.
D52.1 approves this additive R65/R69 field.

A successful write-down additionally stores optional
`recordingDetails.writingDown.sourceSpansSeparated: true` when the actual track
planner separated conflicting spans. Details shows details.sources.overlap only
from that stored fact, never from an opaque ID suffix, an audio level or a reader
regrouping content. The next successful attempt replaces this known fact (and
removes it if no separation ran), retaining unknown future fields. The existing
schema permits additive fields; older apps preserve it. Failed/interrupted jobs
never claim a new completed separation. The corresponding real FIFO queue tests
cover overlapping and ordinary input and stale-fact removal. Mac can mirror this
small diagnostic fact without inferring it from names.
