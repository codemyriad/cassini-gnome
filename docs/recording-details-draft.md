# Recording details — D28 Part A design draft

This draft adds **Recording details…** to the Meeting header menu and a **Details**
link to the Recording page. Both open the same live dialog. English is provisional
until Silvio approves the design and the coordinator publishes `details.*` copy.

The timeline uses saved track offsets and lengths, split at source/selection events.
Browser stream counts count audio sources, never participants. Unknown offsets
have no displayed time range. Missing source-change events are explicitly noted.
Recovered media without browser provenance is not labelled as browser audio;
track evidence does not establish why a source changed. In particular, today's
`lost` event cannot distinguish reload, closed tab, crash or a connection problem.
The meeting service/device name is shown only if it is actually recorded; a title
mentioning Google Meet does not establish the capture method or stream identities.

An already-present optional `end` event can be displayed, with the recorded reason
and timestamp. No end events are written by this change. Older recordings show
**Not recorded for this meeting**; live state comes from the existing controller
and Library scanner, not a `state: recording` file left behind after a crash.

Health includes gap/dropped frame counters, their durations using each track's own
rate, issue kind, and an estimate of microphone DC offset from sampled initial PCM.
The sample is limited to one second per microphone track and 512 KiB in total.
Offsets of at least 512 PCM16 units are called out. This is an observation, not a
measurement of the entire meeting; current speech detection compensates, WAVs are
unchanged. A missing counter is unknown, not a claim of zero lost audio.
If a saved counter lacks a valid sample rate, its frame count is shown without
inventing a duration.

Writing-down model and language come from saved metadata. Speaker separation uses
the speaker-turns sidecar; recognised names come from the Library's resolved speaker
model. Current writers do not persist writing duration or speech-segment count,
so those facts remain explicitly unrecorded. This draft does not add bookkeeping
to transcription or change either recognition pipeline.

All reads run on a bounded, single-flight daemon worker, outside GTK. JSON reads
are limited to 1 MiB per file; the transcript reads only a 64 KiB top-level header,
stopping before the words array. Oversized or unsafe metadata leaves a visible note.
Legacy transcripts whose language follows the word array leave that fact unknown.
Files are opened as nonblocking regular files without following symlinks. The
dialog never writes files, imports an engine or reads the saved-voice store.
Externally opened folders stay bound to their actual location, even if the Library
contains another meeting with the same UUID and folder name.
Copy details is a plain-text whitelist of the displayed facts, including the
folder path and recognised names, without voiceprints, embeddings or raw metadata.
Long timelines show the first 20 spans per side, with the remainder in Copy details.

Part B is untouched: protocol files, event writers, call-end/return grace periods,
browser control messages and fallback behaviour are unchanged.

The synthetic review fixtures contain sparse silent PCM plus a generated DC-only
microphone sample. Their optional `end` entry is reader-test input, not a claim
that released Cassini already writes it. Real GTK screenshots are emitted by
`tests/ui/recording-details.py` into the `ux-boards` artifact:

- `recording-details-browser-fallback.png`
- `recording-details-microphone.png`
- `recording-details-no-events.png`
