# D52 source diagnostics — proposed R65 placement

This draft preserves an optional `recordingDetails.captureRecord` in meeting.json
before finalization. The existing S-12 mapping carries it verbatim inside
`x["codemyriad.io"].meeting.recordingDetails`. No library_v2 core or shared schema
has been edited. The coordinator/Mac must approve the exact shared placement
before this draft is merged. Report copy is proposed separately in R63.

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

Pending: catalog publication and report/Recording UI, shared overlap-lane IDs
(R64), private-data export policy for a shared copy, D49 ownership masking and
initial stand-in transport adoption. These are held in the draft, with evidence
and copy proposals in the pending REPLIES-GNOME append on roy.

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
This addition remains part of the shared R65/R69 proposal awaiting review.
