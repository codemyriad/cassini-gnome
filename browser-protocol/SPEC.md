# Browser recording protocol — SPEC v1.1

## Changelog — v1.1 (2026-10-27)

Amends v1 in place; wire `"version": 1` and session `"version": 1` are unchanged. Formalizes
optional error codes, null-as-absent, integer literals, the closed type set, source enum,
creation-date grammar, and optional ended/endOffsetMs/problemKind/labelSource. Existing
v1 clients may ignore the optional additions. Native and browser writers now share the
same session directory contract; no automatic Opus encoding is required while recording.

Canonical source: https://github.com/codemyriad/cassini-browser-protocol. Changes land here
first; app repositories vendor an exact commit and verify both its Git identity and bytes.

Normative contract for `io.codemyriad.cassini`. Independent of OS, implementation language,
transport launch path, and the separate Cassini Ogg Opus format.

Host names use the reverse domain of codemyriad.io. Pre-release builds used
`com.codemyriad.cassini` and `com.codemyriad.cassini.control`. The extension never looks those up.
A host installer SHOULD delete legacy manifests with those names that it wrote itself (same file
name, same `name` field, `path` pointing to its own host executable) and leave any other file alone.

## Transport and lifecycle

Each message is a native-endian uint32 byte length followed by UTF-8 JSON (little-endian
on supported arm64/x86-64 hosts). Read partial pipe input until complete. stdout contains
only framed responses. One connection owns one session. Each request receives one response
in FIFO order, including pipelined requests. On a protocol/IO error return `error`, finalize
partial audio as interrupted, then exit nonzero without waiting for EOF. EOF before stop
also finalizes as interrupted; EOF after a successful stop exits zero.
Start/stop and filesystem destinations are extension/host decisions, never page commands.

JSON objects reject duplicate members. Unknown extra fields are ignored. Recognized fields
are validated on the message type where they apply. Optional null means absent. Required
null means missing. Integer fields accept integer literals only (no decimal point/exponent,
boolean, or quoted numbers), limited to ±9007199254740991. Finite JSON numbers including
exponents are permitted for offsetMs; NaN and Infinity tokens are invalid JSON.

## Requests and responses

| Request | Required fields | Optional fields | Response |
| --- | --- | --- | --- |
| start | type="start" | version integer 1, title string | started, path string |
| track | type="track", id string, kind string, sampleRate integer, channels integer | label string; clock facts and documentGeneration (see Page trust and browser clocks) | ack |
| audio | type="audio", id string, sequence integer, offsetMs number, pcm base64 string | none | ack |
| stop | type="stop" | problem string | stopped, path string, optional problem string |

Omitted/null version means 1. Unsupported version and unknown request type are errors.
Start is accepted once per connection. Track/audio/stop require an active session.
Kinds: local, remote, media, tab, microphone, application, system. IDs are unique for the
session including ended sources; source formats are immutable. No participant identity is
inferred. Legacy v1 has no track-end message (negotiated multi-stream adds trackEnd). A replaced source may use a new ID;
reversible sender muting should retain its ID and send silence.

An omitted/null title defaults to a host title. An omitted/null label defaults to kind.
Titles are bounded to 500 Unicode characters; labels strip Unicode categories Cc and Cf (including U+200B, preserving private-use Co) and
are bounded to 200 characters. Empty/null/absent stop problem means no interruption unless
IO failure, an empty session or a one-second gap (see Gap ranges) independently requires one.
Empty sessions are interrupted.

PCM is nonempty standard base64 of interleaved signed PCM16 little-endian samples. Acks
follow acceptance/writing, not receipt alone. Sequence starts at 0 per ID and increments
by one per accepted chunk. Offset is the chunk start relative to the session origin.
The first chunk establishes startOffsetMs; there is no leading silence, except that a span opened
at a selection boundary (S-6) starts there and is padded to its first chunk under the same rule.
Compare following start offsets to written frames: round((offset-start)*rate/1000), rounding
half up, tolerance floor(rate/50).
Positive gaps greater than tolerance become silence and a gap range (see Gap ranges). Negative deltas below -tolerance
are errors. A positive gap over 60 seconds is an error. Smaller jitter is tolerated.

`error` responses contain type="error", message string, and optional code string. New
hosts emit codes; clients accept older hosts without codes. Codes: version (unsupported
version/type), state (wrong lifecycle), source (ID/format/metadata type), sequence (wrong
sequence), chunk (PCM/offset/gap), limit (source count), io (storage), framing (JSON/length).
When several errors exist, lifecycle is checked first except start's already-started check
preceding version. Vectors deliberately isolate other errors; wording is not contractual.

## Limits

| Item | Limit |
| --- | --- |
| Native JSON frame | 1–524288 bytes |
| JSON depth | 64 nested object/array containers, counting the root; scalar leaves add no level |
| Sources per session | 64, including ended sources |
| ID | 1–200 UTF-8 bytes |
| Sample rate | integer 8000–192000 Hz |
| Channels | integer 1–2 |
| PCM | 1–262144 decoded bytes, divisible by channels*2 |
| Encoded PCM | at most 349528 UTF-8 bytes |
| offsetMs | finite 0–86400000 |
| Per-source gap | at most 60000 ms beyond current written samples |
| WAV data | at most 2^32-1-36 bytes |
| Extension page packet | 400000 JSON characters |
| Extension native requests | at most 512 in flight, 10-second response timeout |
| Extension source buffer | 10 seconds, four pipelined requests per source |
| Extension worklet batch | 8192 frames, partial tail flushed at stop |

The last four are snapshot resource policies, not requirements on third-party hosts.
Overflow is counted and reported, preserving render-clock gaps instead of compressing time.
A failed page source is retired while other sources continue. A host transport failure ends
the session because its ordered acknowledgements can no longer be trusted.

## Page trust and browser clocks

The relay token is a demultiplexing tag, NOT authentication from the page. Pages can observe
it, supply arbitrary audio/labels, disrupt capture, or spoof page events. The relay restricts
packets to track/audio and supplies fixed interruption strings. Unexpected stopped events
must visibly interrupt the session. Host labels/paths are never executable content.

One AudioContext per meeting frame shares one calibration (D61, extension 0.2.12). The session
origin is a wall-clock Date.now(). The capturing document maps it onto its own performance.now()
once, by reading Date.now() on both sides of one performance.now(), and never uses
performance.timeOrigin, which each document fixes on its own. The render clock is then placed on
that base with getOutputTimestamp (`outputTimestamp`), or, when no positive stamp exists, with a
worklet round trip that reads the render frame between two performance.now() readings
(`roundTrip`). Both place a render frame at the instant the browser says it reaches the output
device, so roundTrip adds the context's outputLatency to the time it read the frame. Calibration
happens before the track is declared. Later offsets come only from
render frames. Different frames/devices remain approximately aligned, not sample-accurate
conference clocks. Local sender mute/suspension produces silence.

Since extension 0.2.12 a browser `track` carries three record-only clock facts. All spans on
one AudioContext repeat the same values. Native tracks omit them.

- `clockMethod`: "outputTimestamp" or "roundTrip". Readers treat absent, null or any other
  value as unknown. Writers never send another value.
- `clockUncertaintyMs`: an integer from 0 to 86400000. It is the part of the error in the
  span's start against the session origin that the extension measures, rounded up: the mapping
  read plus one render quantum, and for roundTrip half the round trip, with one device callback
  (baseLatency) in place of the quantum when that is longer. It is not a bound on what the
  extension cannot measure: the accuracy of the browser's output timestamp and latency
  estimates, and input latency the browser does not report.
- `clockClampMs`: an integer from 1 to 86400000, present only when the calibrated start fell
  before the origin. The extension then moves the whole span later by that many milliseconds,
  once, so chunk spacing stays exact, and clockUncertaintyMs is at least clockClampMs. A start
  more than 86400000 ms early is a fault: the span fails and is retried. Offsets are never
  clamped chunk by chunk; a chunk that would still be negative aborts its span.

Hosts copy the three facts into the browser track entry of session.json as received, and omit
them when absent or invalid. They are record-only: no reader moves or widens a track with them.
An invalid fact is not a source error: the host acknowledges the track and omits only that fact,
keeping the valid ones. The two numbers are integer fields, so 9.0 or 1e3 is invalid
although JSON Schema reads it as an integer. Hosts do not cross-check the facts: each is validated on its own and
copied as received, so a clockClampMs above clockUncertaintyMs is still copied (record-only, D47).
The relation between them binds the writer only. Literal cases: conformance/vectors.json.

Since extension 0.2.12 a `track` may also carry `documentGeneration`: the opaque generation of
the document the span came from, 1 to 200 UTF-8 bytes, the same value status reports
(SPEC-CONTROL.md) and compared only for equality. The extension's background writes it from
the frame that sent the span and replaces any value the page sent. A main frame's span gets
that frame's generation; a subframe's span gets the generation of the main frame it was first
discovered under (the first one not obsolete at one of its discoveries), never a later main
frame's, so a departing subframe's late span cannot name the next document. A generation counts
UTF-8 bytes wherever the extension reads it, so status and `track` agree. The schemas cannot
count bytes: their maxLength 200 is only an outer bound in characters, and the receiver checks
the byte limit, so 200 two-byte characters pass the schema and still count as absent. It is
absent for whole-tab spans, when unknown, and from older extensions. It is a live liveness hint (see Audio transport
liveness) and is never written to session.json. sourceTrackId stays opaque.

## Session directory v1

`session.json` has version=1, UUID id, createdAt, title, source, state, tracks. Source is
chromium, macos, gnome, or recovered. State is recording, completed, or interrupted. Optional
problem and problemKind explain an interruption (empty, capture, io, recovery, gap). Unknown
additional properties are allowed. Writers omit absent optional fields; readers also accept
null. Key order and whitespace are insignificant. New writers use whole-second UTC Z dates;
readers accept fractional seconds and numeric UTC offsets from earlier writers.

Tracks contain id, label, kind, generated safe WAV basename file, sampleRate, channels,
frames, optional startOffsetMs, endOffsetMs, ended, labelSource. Frame count is per channel
and includes inserted silence. ended=true means the writer finalized the source; it does
not establish participant departure. endOffsetMs=startOffsetMs+frames/rate*1000 when known.
labelSource optionally identifies browser, device, user, or recovered; omission means unknown.

WAVs use a 44-byte PCM16 RIFF header. Complete PCM frames on disk are authoritative over
periodically checkpointed frame counts, including a header one append behind after a crash.
Before acknowledging start, write session.json; before each track acknowledgement, checkpoint
the new track metadata. A filesystem error at either step returns `error io` and exits nonzero.
Each file contains only the data chunk after that header. Writers sync audio before metadata,
sync temporary metadata, then rename atomically; platform disk-cache flushes may be stronger.
An interrupted recording is recoverable from its WAVs. Without trustworthy metadata, readers
must state that source labels/start offsets are unknown, not invent synchronization.
Readers reject traversal or symlinks escaping the session directory. Recovery is read-only.

## Optional session annotations (S-6, 2026-10-28)

Session and wire versions remain 1. These are optional on-disk annotations, not
new audio requests. Old folders without them remain valid; readers must not infer
fallbacks, selection changes or source identity from labels or gaps.

Each capture span has its own unique track ID and WAV. Turning a side off finalizes
its tracks (`ended: true`, with `endOffsetMs`); turning it on creates new tracks with
new IDs, WAVs and start offsets on the same session timeline. Never append another
capture source to a previous track. Each concurrent meeting has its own session
folder, events and microphone copy.

A browser track may carry `sourceTrackId`, an opaque nonempty source ID of at most
200 UTF-8 bytes. Resumed spans from the same still-connected source retain this
value while receiving distinct track IDs/WAVs. Transcription groups spans with the
same sourceTrackId into one speaker; absent IDs remain separate sources. Labels
never establish identity. Writers omit sourceTrackId for microphone/system tracks,
which already retain their Me/The others identity. As with other optional fields,
writers omit absent values and readers accept null as absent. Browser tracks may also carry
clockMethod, clockUncertaintyMs and clockClampMs (D61), copied from the track declaration
(see Page trust and browser clocks).

The optional `events` array has at most 1000 objects, in append-only,
non-decreasing `atMs` order. `atMs` is a finite nonnegative number of milliseconds
on the same timeline as track offsets, with zero at session start. Equal times
are allowed. Unknown types and extra fields are ignored by readers.

| type | Required event fields besides atMs/type | Values |
| --- | --- | --- |
| othersSource | source, reason | source: browser, system, none; reason: start, missing, lost, user, restored |
| selection | me, others, reason | me/others booleans, at least one true; reason: start, user |
| end | reason | user, callEnded, tabClosed, pageGone, disk, error, appQuit |

For UI roles, local/microphone tracks are Me; remote/media/tab/system/application
tracks are The others. These are recording roles, not verified participant names.

A browser path missing at start or lost later may fall back to computer sound:
append `othersSource` with source=system and reason=missing/lost, and start a new
system track at that event's atMs. Never fall back to the microphone. D33 permits
same-tab browser return before the120s deadline: append browser/restored and start
new browser spans, checkpointed together, before consuming their PCM. Restored
is legal only after system/missing or system/lost while The others is selected.
The folder, original capture clock and microphone choice remain unchanged. Reuse
sourceTrackId only where the browser actually reports the same opaque source ID;
labels, titles and URLs never establish identity. See SPEC-CONTROL call lifecycle.
Show the fallback explanation only when the newest othersSource reason is missing
or lost. `restored` is not a valid selection reason.

Selection changes finalize/start the affected spans at atMs. Turning The others
off also appends `othersSource` source=none, reason=user. A selection never turns
both sides off. Folders without events have no inferred fallback line. Events are
not copied into Cassini-file exports in this wave; the existing mix/offset and
unknown-offset recovery policies remain in force.

Checkpoint the event and track change together, before acknowledging or using
the new track. If no track changes, write the event immediately. Sync audio, sync
the temporary metadata, then rename atomically as above. A crash must not expose
a new track without the event explaining it, or an event without its new track.
Readers may lose events after the last fully synced checkpoint; they do not infer
them from audio gaps.

An optional terminal `end` is written once when the owning app really stops the
session, including native recordings. `atMs` is the stop boundary on the original
session timeline, not an epoch timestamp; it is at least every finalized known
track end and prior event time. Finalize/sync accepted PCM, then atomically
checkpoint finalized tracks, saved state and the one end event before acknowledging
Stop. A failed stop due to I/O records error where a durable checkpoint is possible;
gaps alone do not change a user Stop into an error. Unknown/old end reasons are
never guessed by a reader. Repeated Stop/close cannot append a second end or
change its reason; no event follows end. Reserve one of the1000 event slots for
it (new source/selection changes at999 events are rejected without modifying the
current source). Never end merely because a browser path switches to fallback.
Legacy folders without end remain valid, and version stays1.

`conformance/session-events/` contains the coordinator/GNOME fallback and browser
off/on fixtures, with hand-written reader expectations and a sparse silent-WAV
generator. Their byte/timeline checks do not prove transactional crash durability
or hardware capture; each host must test its real writer checkpoint boundary.

**Refusals (D58, R79).** The optional top-level `refusals` array records sides that were
requested but never opened, at start or at a later turn-on. A side has *opened* when its
first buffer is admitted, with a 3 s bound from the open attempt (on macOS, when its
`begin()` returns). Before that, any failure (a state change, a bus error, no sample within
3 s, a refused or unanswered browser Start) is a refusal; after it, nothing is. These keep
their existing records and are never refusals: a mid-call failure (P10 `captureOutcome
failed`, track problemKind `capture`/`io`), gaps, writer or disk loss, a user Stop, an
unselected source, a Start the user cancelled or Stop all, the D49 stand-in, a plain native
Record, and setup or connection states (`temporary`, `noBrowser`, `unwritable`, `unsafe`,
`buildMissing`, a hello version mismatch, a missing optional capability).

Each entry is `{atMs, side, code, detail?, message?}`:
- `atMs`: milliseconds on the session timeline, 0 to 86460000 as for `gaps.atMs`. A start
  refusal uses 0, also when it happened before the folder existed. A refused turn-on, and a
  refused resume in an existing folder (`startRejected`/`resumeRefused`, D58 #10), use their
  real, possibly fractional, time.
- `side`, `code` and `detail` are open strings matching `^[A-Za-z]{1,32}$`. Implementers MUST
  apply the pattern to the whole string. JSON Schema patterns are ECMA-262, where `$` does
  not match before a final newline, but Python `re.match`/`re.search` (and so Python
  jsonschema) and ICU/NSRegularExpression do: use `re.fullmatch` in Python and `wholeMatch`,
  or a pattern ending in `\z`, in Swift, so `"busy\n"` is rejected. Known sides: `me`,
  `others`, `browser`. Writers use only four codes: `permissionDenied` (me, others; the OS
  reports the permission denied at that moment, never inferred from silence),
  `deviceUnavailable` (me, others; any other open failure), `startRejected` (browser: Start
  returned `ok:false`) and `startFailed` (browser: no answer, or refused locally; banner only).
  Known details: for startRejected `busy`, `limit`, `noSources`, `gone`, `notWebPage`,
  `pageUnreachable`, `invalid`, `resumeRefused`, `workletBlocked`, `audioBlocked`; for
  startFailed `timeout`, `disconnected`, `busy`, `gone`, `notReady`, `limit`, `noSources`.
  A pattern-valid control result `code` (SPEC-CONTROL, "Result codes") is copied into
  `detail` as is, known or not; any other value is dropped. A result without `code` still
  records `startRejected`, without `detail`.
- `message`: optional raw text from whoever refused (the extension's `error`, a GStreamer
  error's domain, code and debug string, an OS status such as
  `AudioHardwareCreateProcessTap: -50`), never catalog copy, cut to its first 200 Unicode
  code points (not bytes, UTF-16 units or graphemes). An empty text is omitted, as is the
  message for a TCC denial.

The list is in the order things happened, with at most 16 entries: when it is full, each
newer refusal replaces the 16th entry, so the list keeps the first 15 plus the latest. It is
omitted when empty. Checkpoint each entry atomically with any event or track change it
explains. A refused side gets no track. The start `selection` and `othersSource` record what
was requested, not what opened: a system side refused at start still has `othersSource`
system/start and stays selected. A refused turn-on writes no `selection` event and leaves the
selection unchanged.

Writers MUST NOT let refusals change `state`, `problem`, `problemKind`, the `end` reason,
`othersSource` or `captureOutcome`; these follow their existing rules from what did record,
so completed still implies a null problem. A GNOME fallback keeps writing `othersSource`
system/missing; its `browser` refusal at atMs 0 is what tells it apart from a plain native
Record. A `startRejected` with detail `busy` never falls back. If every requested side is
refused nothing records: no folder is published (a folder whose title or notes the user
already typed is kept), and the refusals are shown only in the start banner.

Readers are lenient: a `refusals` value that is not a list is ignored as a whole, a
malformed entry is skipped, and unknown members are preserved. An entry is malformed exactly
when the schema's item rejects it with the patterns applied to the whole string: not an
object; `atMs` missing, not a number or out of range; `side` or `code` missing or not
matching; `detail` or `message` present, not null and invalid (a null one means absent).
Details prints one line per distinct line, in first-occurrence order, so repeated entries
show once. An unknown code, and a stored `startFailed`, show `details.sources.failed` for
that side; an unknown side is not shown and an unknown detail is ignored. A refusal line
replaces `details.sources.notSelected` for its role; a `browser` refusal counts for The
others, so it also hides `notSelected` for `others`. `details.sources.empty` is printed only
when no other Audio sources line is shown at all (no source rows, no refusal lines, no
`notSelected` lines). Copy details keeps every well-formed entry, in stored order, exactly as
stored: no merging, no re-cut and no Unicode normalization, counting code points throughout. `recordingDetails.captureRecord.refusals` in
meeting.json is a verbatim copy. Session version stays 1. Shared cases, oracle and reader
lines: `conformance/refusals/`.

## Browser compatibility of this snapshot

The native messaging/offscreen/tab-capture baseline remains Chrome 116. getContexts is used
instead of the Chrome-150-only offscreen.hasDocument. The loaded scenarios are validated on
Chromium 149.0.7827.55, not a claim that every supported browser version was executed.
match_about_blank covers inherited blank frames; match_origin_as_fallback adds broader
origin-based frame matching on browsers implementing it (Chrome's scripting counterpart is
119+). use_dynamic_url reduces stable-resource fingerprinting on Chrome 130+ and is ignored
on earlier Chrome releases. It does not authenticate page messages. The offscreen worklet
uses an extension-relative URL; MAIN-world imports receive dynamic URLs from runtime.getURL.


## Native recorder session requirements (v1.1)

Both native recorders MUST write `Recording-<UUID>/session.json` and one PCM16 WAV per
source, exactly as described above. macOS uses `source: "macos"`; GNOME uses `source: "gnome"`.
Microphone and system output remain separate tracks (`kind: "microphone"` / `"system"`);
selected application output uses `"application"`. Channel count describes that source's
format, not multiplexed unrelated sources. An Opus file is an export, not the capture folder.

Native sample clocks establish each source's start offset and continuous frame counts;
clock-drift diagnostics must not be mistaken for browser sequence errors. Device/backend
metadata may be added as optional fields. Loaders MUST accept both native source values and
the same safe WAV paths, dates, frame counts, interruption states and optional metadata as
browser sessions. `conformance/native-session/` is a complete deterministic two-source
fixture (440 Hz microphone at 0 ms; 880 Hz system stereo at 125 ms), with macOS and GNOME
manifest variants. It is a synthetic format fixture, not proof of hardware capture.


## Executable JSON examples and schema bounds

See [start](conformance/examples/start.json), [track](conformance/examples/track.json),
[audio](conformance/examples/audio.json), [stop](conformance/examples/stop.json),
[responses](conformance/examples/responses.json), and [session](conformance/examples/session.json).
For example, a complete start request is:

```json
{"type":"start","version":1,"title":"Example meeting"}
```

Schema lengths count Unicode code points; ID limits additionally count UTF-8 bytes.
endOffsetMs is bounded at 86460000 ms (24 hours plus one minute), allowing the final chunk
after the latest legal start offset. A completed session has no non-null problem/problemKind;
an interrupted session has a nonempty problem. Response examples are complete responses;
vector expect objects are partial matchers, not full responses.

After `stopped`, any further request is a lifecycle error: answer `error` with code `state`,
leave the completed session unchanged, and exit nonzero without waiting for EOF. EOF alone
after `stopped` exits zero.

An active recording's path/mode/tab/origin is held in extension session storage after the
host start acknowledgement. Worker restart reports interruption with that path and clears
the stale badge; it does not claim that unacknowledged audio survived. A worker-crashed tab
capture document is closed to release the capture stream.


## Native host-time discontinuities (v1.1 clarification)

After the first-buffer anchor, samples drive continuity. A forward jump between native
input buffers of at least one second beyond the previous buffer's duration becomes counted
silence. Compute `lagMs = currentHostMs - previousHostMs - previousInputFrames/rate*1000`,
round its positive duration to sample frames, and insert it when at least `rate` frames.
Use input-buffer host stamps, not callback delivery/arrival time. This per-buffer difference
protects smooth device drift: comparing absolute session-start lag would incorrectly count
10 hours at +50 ppm (1.8 seconds) as missing audio. Smaller/negative divergence is diagnostic;
it must not reorder samples or silently compress a forward discontinuity.

`gapFrames` and `droppedFrames` are optional per-track nonnegative integer counts.
`gapFrames` counts padding written for missing input time, excluding selection prefixes, and is
included in the WAV/manifest frames. `droppedFrames` counts frames that existed but were not
written, by the host writer or the producer, with no position. A producer drop inside a span
also appears as padding, so the two counts are never added together. Report gap loss with
session problemKind=`gap` only when one non-prefix padding write lasts at least one second (see
Gap ranges); writer loss takes problemKind=`io`. Both preserve available audio and silence.
The complete `conformance/native-session-gap/` fixture contains continuous mic audio and a
one-second system gap, with both macos/gnome manifest variants.

## Gap ranges and presence (D52.2, P7)

Every writer records where it wrote padding: per track, `gaps: [{atMs, durationMs, frames, kind}]`.
New writers always write the key, so `gaps: []` means no gaps and a missing key means unknown.
- `atMs` is on the session timeline (same as offsetMs/startOffsetMs/events): the position of the
  first padded frame in the file, startOffsetMs + framesBefore/rate*1000. Absorbed jitter means
  this can differ from the wire offset by up to the tolerance. Native tracks with host-clock
  anchors use the host time at which the gap began (the next buffer's stamp minus the gap), which
  the anchor mapping approximates. `frames` is the exact integer padding at the track rate.
- Ranges are half-open, sorted and never coalesced: one entry per padding write, even when
  only a little received audio separates two of them.
- `kind` is diagnostic and open. Host-derived kinds: `jump` (browser offset jump; overflow,
  suspension and stalls look the same on the wire), `input` (native host-time gap), `prefix`
  (selection boundary to the first frame, browser or native, never in gapFrames), `writer` (below). `dropped` and `suspended` are
  reserved for a later producer tag. Readers treat every kind as absence.
- Budget: at most 1000 ranges per track, then one `{atMs, kind:"overflow"}` at the next gap,
  meaning presence is unknown from there to the track end. `gapFrames` keeps counting. Never
  truncate silently, evict earlier ranges or merge ranges to stay within the budget.
- A droppedFrames count never becomes a range. Mid-span producer drops already appear as a
  `jump`; drops after the last written frame or between spans are outside every extent.
  Producer drop counts appear only in recording details: they never change state or problemKind.
- A host writer that pads its own lost frames in place (instead of failing the source) records
  that padding as a `writer` range. It counts in droppedFrames, not gapFrames, and never sets the
  gap flag; host write loss is reported as io.
- Writers record ranges whether or not multi-stream was negotiated: these are host facts.
- Gap flag: only a `jump` or `input` padding write of at least one second (frames >= rate),
  including one written after the overflow marker, makes the session interrupted with
  problemKind `gap`; io and capture take precedence. Folders without `gaps` (or with null) keep
  the older reading, any gapFrames > 0.

Presence comes only from span extents minus `gaps`, intersected with othersSource ownership.
Zero samples are not evidence of absence: a muted sender's zeros are present audio. Presence
never reads PCM. An `overflow` marker or a missing/null `gaps` makes the affected time unknown,
neither present nor absent. On a 16 kHz session grid a window [a,b) ms covers samples
[ceil(16a - 1e-6), ceil(16b - 1e-6)); the epsilon absorbs floating-point noise in stored times. The same `gaps` are carried into recordingDetails.captureRecord.tracks[]
and the Cassini file's x["codemyriad.io"].tracks[]. See conformance/gaps/.

## Multi-stream capture (D52, extension 0.2.4; draft PR)

This additive mode keeps version 1 and every legacy conversation. Start requests
`features:["source-outcomes-v1"]`; `started.features` acknowledges support. Only
then may the extension send `sources`, `trackEnd` and `stop.discoveredSources`. An older
host continues to receive its original packet types.

`track.sourceTrackId` is an optional opaque ID, bounded to 200 UTF-8 bytes. It
comes from a document's receiver/transceiver root (sender root for local browser
tracks), never from a label. Replacement/resume spans use fresh `id` values and
the same source key and label. Native microphone/system tracks omit it. Live or
stored overlapping spans with one key must be refused rather than summed.

A negotiated `trackEnd` carries id, reason (ended/replaced/retry/stopped/failed/
callChanged), droppedFrames and endOffsetMs (nullable before an audio anchor).
The host derives the authoritative stored end from admitted samples. It adds droppedFrames
to the latest span that recorded that wire ID, even after deselection, never replacing an
earlier count; a source that was never recorded stores none. An error
with `scope:source` and id refuses only that source request: other tracks and
this native connection remain usable. Framing/version/connection failures stay
session failures. Repeated identical track declarations are idempotent.

Stop carries its captureId; a mismatched negotiated stop is refused without
ending the current capture. The discoveredSources summary retains sources that
failed before opening a WAV. Each source has sourceTrackId, kind, label and a
sticky failed flag. Recorded samples, including zeros/DC, count as received;
inserted gap padding does not. The draft's hand-written outcome vectors propose
neverAudio for zero received frames, lowAudio below 1000 ms, captured otherwise,
and failed for a sticky source error. R63 requests coordinator review of that
cutoff/shape. Per-track captureOutcome/receivedFrames and the top-level
captureOutcome.sources reconcile the result; io takes precedence over capture.
Diagnostic source lists include refused/never-opened sources and are not capped
at the 64 audio-track allocation limit. Their serialized requests remain bounded
by the existing native-message framing limit; identities/labels retain their
per-field bounds. No diagnostic entry allocates a WAV.

`sources` carries a nonempty `discoveredSources` array with the same record shape
as Stop. Each discovery is sent and durably ACKed before attempting its first
audio attachment; a source failure sends a sticky update immediately. Readers
merge records by sourceTrackId and never clear an earlier failed flag. Reject a
malformed batch with scope=command, without closing the connection or allocating
WAVs. The whole batch is validated before its records are changed. Native request
framing remains the size bound; writers stream bounded UTF-8 batches rather than
pruning at 64 or 256 sources. ACK means the capture record is checkpointed, even
when no WAV exists yet. The extension drains outstanding source updates before
its terminal Stop. Its final summary may be empty when all updates were ACKed.

Desktop Stop may finish native capture before an unreachable extension responds;
it must not wait indefinitely for that final summary or depend on it to discover
sources that already failed. A source packet after native finalization is refused
with scope=command and code=state; it cannot rewrite a published meeting. Announced
sources stay in the durable capture record across EOF or a native-first Stop.
A matching Stop with an empty summary may still ACK as stopped without changing
the saved files. A scoped state error retires the producer without retries or a
false failed-source update; native-first Stop is an intentional end.
Unchanged, already-ACKed discoveries do not generate periodic source writes.
The producer's span budget counts successful track ACKs, not IDs generated for
attachments that failed before a WAV was opened; retries cannot spend that budget.

Source ownership is half-open at each othersSource.atMs. Outgoing source audio
is masked at the boundary before recognition, mixing and export. This is not a
transcript display rule. `callConnected` moves an enabled others side from the
initial system/missing stand-in to browser tracks (D49). Existing lost/restored
semantics stay intact: a resume-capture-v1 attach onto a system/lost fallback
(its audio transport closed, D52.4) writes browser/restored. See conformance/multistream/ownership.json.

The 64-span limit counts accepted declarations plus declarations currently in
flight across every frame. The worker reserves a slot synchronously before
forwarding a new track, commits it on ACK, and releases it on refusal. Failed
worklet/context initialization consumes no slot. A limit refusal retires that
live track until a new capture; it never schedules endless allocation retries or
repeats the same warning on discovery ticks.

Receiver roots and all their clones remain receiver sources even when rendered
in audio/video elements. They cannot enter through the media-element path:
connected-peer, receiver direction, generation and inbound-RTP proof govern
admission. A main-frame address boundary is fenced once whether browser URL
notification or page discovery arrives first. Old-generation discoveries cannot
vote in the new call; a child-frame generation change cannot end its parent call.

A transient attach/recovery warning does not mark a source failed. Replacement
spans after recovery close the previous span with reason=retry. Successful PCM
ACKs clear the retrying state; merely opening a worklet does not. If Stop arrives
while a source is still retrying, the producer checkpoints its failed discovery
before Stop. Terminal source refusals and durable native I/O failures remain
sticky; recovery never erases recorded loss. The optional internal relay warning
flag transient distinguishes a pending retry from such terminal loss.

During a page reload, present with zero sources is not proof of return: keep the
original 120-second return deadline, start the fallback after two seconds, and
end with pageGone at that deadline unless a fresh positive inventory restores
browser tracks. See the executable lifecycle vectors.

A span closes at its trackEnd admission barrier. The worker remembers closed IDs
and refuses late audio, repeated declarations and repeated endings locally,
without failing that source's healthy replacement or forwarding to legacy hosts.
Undeclared/refused IDs cannot send audio. A producer drains its admitted PCM and
final worklet flush before trackEnd, then detaches and fences the worklet callback.
Late ACKs from a failed old span do not mark a replacement attempt healthy.

A scope=command, code=state refusal from a finalized native capture retires
both separate-track and whole-tab sessions, including their badge and offscreen
document. A locally fenced closed span only retires that producer; it cannot end
unrelated sources. Source-update refusals do not skip the terminal native Stop.
Test writers refuse track, audio and trackEnd after finalization and cannot
reopen an ended span or rewrite published metadata with its repeated ending.

## Optional native resume (D52.1 / R67)

The opt-in management `resume-capture-v1` operation uses a first native packet
with `type:"resume"`, version1, independent:true, the active captureId, original
timelineOriginMs, selected callGeneration and requested features. It must attach
only to that exact active native stand-in. It must not create a folder, reset
its clock or alter its selection/microphone. Unknown/refused resume requests
fail before any file is created. A stopped/changed target is an error, never a
fresh Start. The handler's target validation and Stop share one transaction.
Explicit successful proof is a `started` response with resumed:true, the exact
captureId/timelineOriginMs/callGeneration, path and resume-capture-v1 feature.
See SPEC-CONTROL.md and conformance/control/RESUME-CAPTURE.md for cancellation,
interleavings and the shared seam fixture. Existing ordinary Start is unchanged.
Since extension 0.2.11 the resume packet also carries the control command's
requestId as `requestId` (same domain as the control requestId), so a host can
tell which ask a refused resume answers. Hosts that do not use it ignore it.
A host that keys on it gives every resume ask a fresh requestId and never
reuses it for another ask, on the same or another control connection, after a
reconnect or an app restart: the audio pipe names no connection, and a late
packet can follow a timeout or a closed connection. A UUID per ask is the
simple way. A late refusal whose ask is already answered or
unknown records nothing.


### Source retirement during an active capture (extension0.2.8)

An ended/failed physical span in a still-active capture returns error with
code:state, scope:source, id and retired:true. It does not mean the whole capture
was saved. Producers retire only that span and may retry its live source with a
fresh span ID. Native-first capture finalization returns scope:command,
code:state, without retired:true, and stops the capture transport intentionally.
Older source/command state errors without the marker keep their historical
finalized-capture meaning, except for a span the extension already fenced after
a prior source refusal. Its already-in-flight state replies and retry cleanup
are local retirement, not evidence that healthy sources or the microphone ended.

Propagate retired:true through native errors, relay ACKs, page and offscreen
capture. TrackEnd cleanup for an already retired span is harmless. A source I/O
error fences its span before later chunks are forwarded. Late ACKs/errors from
that span cannot mark its replacement healthy or stop another source. The live
root keeps its ordinary retry schedule; capture-wide and manifest/directory
failures retain their existing fatal policy.


## Audio transport liveness (D52.4, app side)

The extension already closes a PCM port whose packet is not acknowledged within
10 seconds. These app-side rules let the app notice a frozen browser or audio
relay that keeps the transport open but silent. Times are monotonic.

- `PCM_FRESH = 2 s`: `pcmLive` means admitted browser/remote PCM arrived on the
  recording's audio transport within the last 2 seconds. The native microphone
  is never that evidence. Both apps use this window.
- `PCM_STALE_AFTER = 45 s` (SHOULD): close a recording's audio transport after
  45 seconds without any packet on it. The watchdog is armed only while that
  transport has an open remote span (declared, not yet ended by trackEnd or
  stop) that has already delivered PCM. It is re-evaluated, not sticky: before
  the first remote PCM, and after the last such span ends, nothing can go stale.
  Every frame received on that audio transport counts toward this deadline:
  PCM (`audio`), `track`, `sources` and `trackEnd` alike (D52.5); frames on
  the control connection do not. The comparison is normative: an app that
  runs this watchdog MUST treat the transport as stale exactly when it is
  armed and `now - lastAudioTransportFrame >= 45 s` (so 44.999 s after the
  last frame it is still open and at 45.000 s it is closed), never earlier,
  and SHOULD close it within one policy tick. A frame arriving at or after
  that deadline is too late. A span whose document is gone does not arm this
  watchdog (D57): when the recording tab's status reports pageState
  `navigating`, every open span is retired, and so is any span declared before
  the tab is next reported `present` or with a new documentGeneration; when the
  documentGeneration changes without navigating, the spans declared up to the
  last status that still reported the old generation are retired; a retired
  span's PCM is still recorded but never re-arms it, and a span declared after
  those points, even before its own documentGeneration is reported, stays
  armed (`documentChanged` frames). The first documentGeneration reported for
  the recording, with no earlier one to compare, counts as new: it closes an
  open departing window (unless it arrives with pageState `navigating`) and
  retires nothing. Since extension 0.2.12 a span may carry its documentGeneration
  on `track` (D61). The app keeps the set of departed generations: on
  navigating, the current documentGeneration joins it, and so does the
  generation of every open span; when the documentGeneration changes from a
  known one, the old one joins it, and so does the generation of every open
  span other than the new one. A span with a documentGeneration is dormant
  exactly when its generation is in that set, evaluated at every check; the
  declaration-order rules above apply only to spans without one, and a
  generation that is not 1 to 200 UTF-8 bytes counts as absent. No generation
  ever leaves the set, so the spans of a document restored from the
  back/forward cache stay dormant: that only delays stale detection. Track IDs
  are unique for the session, ended spans included: a repeated `track` for an
  id already declared on the transport changes no span, which keeps its first
  declaration and generation, and its frame still counts as activity. The
  host's reply follows the track rules: an identical repeat of an open id is
  idempotent, and a repeat of an ended id is refused. A track's
  generation is not news about the document: only status and meetings rows
  move the current one. A status that arrives after a newer document's span
  can retire that span; its PCM is still recorded. The app applies these
  rules in its own receipt order across
  the control connection and the audio transport: a status and an audio frame
  are ordered by when the app received them, never by extension timestamps or
  by reordering one socket against the other; the vectors' frame order is that
  receipt order. Literal cases with an independent oracle:
  `conformance/multistream/stale-watchdog.json` (`stale_watchdog.py`).
  Closing uses the app's existing EOF path, so
  the recording keeps running and falls back to computer sound with
  `othersSource` reason lost (SPEC-CONTROL.md, D52.4 Q3). Rationale (D52.5
  amendment): the extension's source retry backs off 1, 5, 15 and 30 seconds
  and the app cannot see that retry state. 30 + the extension's 10-second
  acknowledgement limit is 40 s, and with the `>=` comparison a 40 s deadline
  left no margin, so a healthy retry could lose the race; 45 s adds a 5-second
  margin and stays below the 60-second span cut.
- `WRITE_DEADLINE = 10 s` (SHOULD): bound every socket/pipe write to the browser,
  on both the audio and control connections, to 10 seconds. A peer that stops
  reading is closed, and the close must not wait behind the stuck write.
