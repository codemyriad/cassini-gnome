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

An already-present optional session `end` event can be displayed. No session end
or other protocol events are written by this change. New manual Stops save an
app-owned end fact in `meeting.json` after audio finalization; native disk/error
stops and Stop-and-save on exit also save the reason where known. A capture error
takes precedence over the requested stop reason. Older recordings without an end
fact show **Not recorded for this meeting**. Live state comes from the controller
and Library scanner, not a `state: recording` file left behind after a crash.

Health includes gap/dropped frame counters, their durations using each track's own
rate, issue kind, and an estimate of microphone DC offset from sampled initial PCM.
The sample is limited to one second per microphone track and 512 KiB in total.
Offsets of at least 512 PCM16 units are called out. This is an observation, not a
measurement of the entire meeting; current speech detection compensates, WAVs are
unchanged. A missing counter is unknown, not a claim of zero lost audio. Native-only rows
whose counters/issue kind were never provided are omitted. A native system-sound
span never claims that browser audio was missing.
If a saved counter lacks a valid sample rate, its frame count is shown without
inventing a duration.

Writing-down model and language come from saved metadata; language codes use a
human name. During capture those rows say **After the recording**. Recognised
names come from the Library's resolved speaker model. New successful jobs record
wall duration, whether they resumed, the actual speech-segment count when known,
and explicit separation facts. Old cached speaker turns alone do not establish
those facts; older meetings keep **Not recorded for this meeting** for them.
No recognition, audio, VAD, diarization or voiceprint input changes.

## Optional app-owned metadata (D28 Part A draft)

The on-disk version stays integer `1`. The optional top-level `recordingDetails`
object is defined in `src/cassini_viewer/meeting.schema.json`; all other root and
nested unknown members remain preserved. This is bookkeeping in `meeting.json`,
not an extension of `session.json` or the browser protocol.

| Placement | Type and meaning |
| --- | --- |
| `recordingDetails.end.reason` | `user`, `disk`, `error` or `appQuit` for current writers. The schema/reader also accepts a recorded `callEnded`, `tabClosed` or `pageGone`; this change does not start writing those. |
| `recordingDetails.end.atMs` | Optional integer: final saved audio endpoint, exactly floor(offset + frames × 1000 / rate), maximized over tracks with known offsets. It is not the button-press clock time. Unknown offsets are never assumed to be aligned or written back. |
| `recordingDetails.writingDown.wallDurationMs` | Nonnegative integer milliseconds measured by a monotonic wall clock for the successful attempt. Starts when its FIFO job runs; excludes queue wait and earlier interrupted attempts. Includes worker setup, recognition, optional separation/voice extraction, and publication up to the final done write. |
| `recordingDetails.writingDown.resumed` | Boolean: the attempt resumed a previously queued/running job. The displayed duration is labelled “resumed; this run” when true. An explicit retry of a failed job is a new attempt. |
| `recordingDetails.writingDown.speechSegments` | Optional nonnegative integer from the worker's actual segment count, including zero. Omitted when not reported. |
| `recordingDetails.writingDown.speakerSeparation.status` | `completed`, `partial`, `skipped` or `failed`. Failure keeps the completed words. |
| `…speakerSeparation.modelId` / `modelRevision` | The pinned model id and full 64-character revision. Required for completed/partial separation; copied in full in the dialog/Copy details. |
| `…speakerSeparation.speakersFound` | Nonnegative integer, required for completed/partial. Counts distinct native (group, voice) pairs, before names or UX16 merges. |
| `…speakerSeparation.reused` | Boolean, required for completed/partial: all returned groups reused validated cached speaker turns. Mixed inference/cache is false. |
| `…speakerSeparation.reason` | Required for skipped/failed; one of `modelsNotInstalled`, `modelNotReady`, `unavailable`, `notApplicable`, `tooShort`, `processingFailed`. Partial may also carry `processingFailed`. These are facts about that attempt, not today's model state. |

Writing facts are published atomically with transcript status `done`, after optional
attribution and voice extraction. The writer rereads under the metadata lock so a
concurrent title/name/note/end edit is retained. Cancelled/failed jobs do not claim
new successful timing facts. Stop bookkeeping runs off GTK and cannot turn saved
audio into a failed Stop if its optional metadata write fails. Native WAVs and
capture manifests are unchanged by the new facts, and repeated identical Stop
bookkeeping does not increment the metadata revision.

The dialog follows its content’s natural size as the asynchronous report arrives;
short reports are shorter than long timelines. Facts use a grid so GTK can negotiate the height-for-width columns without asking
for their width at a viewport height below their minimum. Unchanged reports (and
unchanged read errors) do not touch the widget tree. Ordinary live updates reuse
the existing labels and only change differing values; the body is rebuilt only
when its visible structure changes, such as a new capture span or extra note.
This keeps focus, text selection and scrolling stable during recording ticks.

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

The GTK interaction test covers long per-side timelines and a real NativeRecorder
with generated live GStreamer sources (no host audio). It waits for three actual
reader timer completions and checks that saved reports compare equal, live end
times advance, body/label identities stay fixed, and focus/scroll position survive.
A parent process replays the child's complete stderr and fails on any Gtk warning
or critical between dialog-open/closed markers. Startup diagnostics outside those
scopes are still printed. No G_DEBUG/log suppression is used. A mutation restoring
the old facts box makes the new warning guard fail after the UI assertions pass.

The GTK regression sends real Escape key events in private Xvfb for saved, fixture-live
and generated-live recordings. Escape closes only the dialog; real recording
continues. It compares actual content heights and retains the existing warning,
equal-report, focus and scrolling checks.

## QA checklist (coordinator report 2026-10-30, items 1–9)

1. Live/saving Writing down rows say “After the recording”, verified by the report test.
2. Manual Stop records optional app-owned end facts without changing session events; an existing session end event wins when reading. Older unknown ends stay unknown.
3. The bounded transcript-header reader displays language names (including English), without loading a long word array.
4. Native system audio never claims a browser was missing; only a matching browser capture/event explains a browser fallback.
5. Native health counters absent from the saved data are hidden; explicitly reported counters are displayed.
6. Successful new jobs record the actual separation outcome, including models not installed. Older meetings do not acquire inferred processing facts.
7. Details and Change what is recorded share the recording-change button style.
8. The dialog follows natural content size. Its facts grid negotiates width without the former constrained-height GtkBox warnings.
9. Real Escape closes saved, fixture-live and generated-live dialogs while recording continues.

Refreshed against main `5f48fbb`: all seven Meson checks pass (1123 source tests,
one unavailable-locale skip). The real dialog test passes seven warning-guarded
open scopes, including many spans, live capture, unchanged reports and preserved
focus/scroll. Rename menus/keys and the D37 journey test pass alongside it.
The review PNGs remain 1120×720. This remains a design draft for Silvio.
