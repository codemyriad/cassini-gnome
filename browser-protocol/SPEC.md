# Browser recording protocol v1

Normative contract for `com.codemyriad.cassini`. Independent of OS, implementation language,
transport launch path, and the separate Cassini Ogg Opus format.

## Transport and lifecycle

Each message is a native-endian uint32 byte length followed by UTF-8 JSON (little-endian
on supported arm64/x86-64 hosts). Read partial pipe input until complete. stdout contains
only framed responses. One connection owns one session. Each request receives one response
in FIFO order, including pipelined requests. On a protocol/IO error return `error`, finalize
partial audio as interrupted, then exit. EOF before stop also finalizes as interrupted.
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
| track | type="track", id string, kind string, sampleRate integer, channels integer | label string | ack |
| audio | type="audio", id string, sequence integer, offsetMs number, pcm base64 string | none | ack |
| stop | type="stop" | problem string | stopped, path string, optional problem string |

Omitted/null version means 1. Unsupported version and unknown request type are errors.
Start is accepted once per connection. Track/audio/stop require an active session.
Kinds: local, remote, media, tab, microphone, application, system. IDs are unique for the
session including ended sources; source formats are immutable. No participant identity is
inferred. There is no track-end wire message in v1. A replaced source may use a new ID;
reversible sender muting should retain its ID and send silence.

An omitted/null title defaults to a host title. An omitted/null label defaults to kind.
Titles are bounded to 500 Unicode characters; labels strip Unicode control characters and
are bounded to 200 characters. Empty/null/absent stop problem means no interruption unless
IO failure or an empty session independently requires one. Empty sessions are interrupted.

PCM is nonempty standard base64 of interleaved signed PCM16 little-endian samples. Acks
follow acceptance/writing, not receipt alone. Sequence starts at 0 per ID and increments
by one per accepted chunk. Offset is the chunk start relative to the session origin.
The first chunk establishes startOffsetMs; there is no leading silence. Compare following
start offsets to written frames: round((offset-start)*rate/1000), tolerance floor(rate/50).
Positive gaps greater than tolerance become silence. Negative deltas below -tolerance
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
| JSON depth | 64 levels |
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

One AudioContext per meeting frame shares a calibration from the first worklet batch using
getOutputTimestamp, or first-batch arrival minus rendered duration as fallback. Later offsets
come only from render frames. Different frames/devices remain approximately aligned, not
sample-accurate conference clocks. Local sender mute/suspension produces silence.

## Session directory v1

`session.json` has version=1, UUID id, createdAt, title, source, state, tracks. Source is
chromium, macos, gnome, or recovered. State is recording, completed, or interrupted. Optional
problem and problemKind explain an interruption (empty, capture, io, recovery). Unknown
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
Each file contains only the data chunk after that header. Writers sync audio before metadata,
sync temporary metadata, then rename atomically; platform disk-cache flushes may be stronger.
An interrupted recording is recoverable from its WAVs. Without trustworthy metadata, readers
must state that source labels/start offsets are unknown, not invent synchronization.
Readers reject traversal or symlinks escaping the session directory. Recovery is read-only.

## Browser compatibility of this snapshot

The native messaging/offscreen/tab-capture baseline remains Chrome 116. getContexts is used
instead of the Chrome-150-only offscreen.hasDocument. The loaded scenarios are validated on
Chromium 149.0.7827.55, not a claim that every supported browser version was executed.
match_about_blank covers inherited blank frames; match_origin_as_fallback adds broader
origin-based frame matching on browsers implementing it (Chrome's scripting counterpart is
119+). use_dynamic_url reduces stable-resource fingerprinting on Chrome 130+ and is ignored
on earlier Chrome releases. It does not authenticate page messages. The offscreen worklet
uses an extension-relative URL; MAIN-world imports receive dynamic URLs from runtime.getURL.
