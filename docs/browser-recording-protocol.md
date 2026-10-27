# Browser recording protocol v1

This is the portable interface between `BrowserExtension/` and a local recorder. The Mac
implementation is in `Sources/CassiniRecording/BrowserProtocol.swift`; a Linux host can
implement the same messages in Python/GStreamer. It does not depend on Swift, AppKit,
Core Audio, or a local TCP server. The native host name is **`com.codemyriad.cassini`**.

## Compatibility

Required v1 fields and message ordering are stable for the GNOME implementation to adopt.
Additional optional fields may be introduced. Ignore unknown optional fields. A `start`
without `version` is accepted as v1 for compatibility with the initial prototype; explicit
versions other than `1` must fail. Breaking changes require a new protocol version.
This recording-session format is distinct from the Cassini portable Ogg Opus format.

## Transport and lifecycle

Use Chrome Native Messaging: a native-endian unsigned 32-bit JSON byte length, followed
by UTF-8 JSON. On supported Mac/Linux x86_64/arm64 systems this length is little-endian.
Handle partial reads. The host uses stdout exclusively for framed responses and stderr
for diagnostics. The extension maintains one `connectNative` port per recording.

Every request receives exactly one response, in request order. There are no unsolicited
host events in v1. The extension correlates FIFO responses, not request IDs. Each source
has bounded buffering, and the host connection has at most 128 pending requests. The host
must reject oversized frames before allocating: this implementation uses a 512 KiB cap.

The extension starts recording only from its controls. MAIN-world page code supplies
`track` and `audio` packets through an isolated-world relay; it cannot issue host start/stop
commands or choose output paths. Page-supplied audio/labels are untrusted content, not
verified participant identities. The host allowlists the installed extension ID.

EOF after a start but before stop finalizes available audio as interrupted. A host error
returns one `error` response for the failing request, finalizes partial audio, then exits.
Successful stop returns `stopped` after finalization, then the host may exit. Never lose
already-written audio just because the extension/tab/browser disconnects.

## Messages

Start, extension → host:

```json
{"type":"start","version":1,"title":"Weekly meeting"}
```

Response:

```json
{"type":"started","path":"/absolute/path/to/recording-directory"}
```

The host chooses the directory; no incoming message may choose a filesystem path. Titles
are display metadata. A host instance accepts exactly one recording.

Register a source:

```json
{"type":"track","id":"opaque-unique-id","label":"remote: browser label","kind":"remote","sampleRate":48000,"channels":1}
```

Response: `{"type":"ack"}`.

IDs are session-scoped, unique, nonempty strings of at most 200 UTF-8 bytes. Labels are
untrusted display text (the Mac host truncates to 500 characters). Accepted kinds are
`local`, `remote`, `media`, `tab`, `microphone`, `application`, and `system`. The extension
currently emits the first four. Accept sample rates 8000–192000 Hz and 1–2 channels.
Limit a session to 64 registered sources, including ended sources. Source formats are
immutable. Replaced tracks get new IDs; no assumption about participant identity follows
from either track ID or browser label. There is no track-end message in v1: a track simply
stops producing chunks; the host finalizes all tracks at session stop.

Audio:

```json
{"type":"audio","id":"opaque-unique-id","sequence":0,"offsetMs":123.5,"pcm":"AQACAA=="}
```

Response: `{"type":"ack"}` **after the samples have been accepted/written**.

- PCM is standard base64 of interleaved **signed 16-bit little-endian** samples. The example
  is two mono samples, 1 and 2. No container headers are included.
- Sequence begins at zero for each source and increments by one per message. Missing,
  duplicate and reordered sequences are errors; do not silently compress a missing interval.
- `offsetMs` is the start of this chunk in milliseconds from a shared session origin.
  It is a finite nonnegative number, capped at 24 hours by this implementation.
- AudioWorklet timestamps come from the audio render frame clock, translated to the shared
  session origin. Do not timestamp by message receipt. Separate browser contexts are aligned
  approximately to this origin, not guaranteed sample-accurate conference clocks.
- Nonempty PCM must be frame-aligned (`channels * 2` bytes). Mac accepts up to 262144 decoded
  bytes per message. The extension normally batches 2048 frames and flushes partial tails.
- A source's first chunk establishes its `startOffsetMs`; do not write leading silence for
  the whole session. Subsequent significant gaps must remain represented in the timeline.
  Mac inserts silence for positive gaps above 20 ms, rejects overlaps above 20 ms, and rejects
  gaps above 60 seconds. Smaller clock jitter is tolerated. Other hosts should preserve the
  same timeline semantics and reject corrupted/out-of-order data.

Stop:

```json
{"type":"stop"}
```

Or `{"type":"stop","problem":"The meeting frame closed."}` to save an interrupted session.
The extension flushes audio and drains acknowledgements before issuing stop.

Response:

```json
{"type":"stopped","path":"/absolute/path/to/recording-directory"}
```

An optional `problem` string in the response describes an interruption, including a session
with no audio samples. The extension displays it. On failure:

```json
{"type":"error","message":"Human-readable error"}
```

## Local session directory

Mac saves each source as a PCM WAV and writes `session.json` atomically:

```json
{
  "version": 1,
  "id": "E5B7AE4F-127B-4279-9D22-8C8CAB73B66A",
  "createdAt": "2026-10-27T09:00:00Z",
  "title": "Weekly meeting",
  "source": "chromium",
  "state": "completed",
  "tracks": [{
    "id": "opaque-unique-id", "label": "remote: browser label", "kind": "remote",
    "file": "track-E3883AB2-719B-44CB-B9B8-CCFF3D2DACCF.wav",
    "sampleRate": 48000, "channels": 1, "startOffsetMs": 123.5, "frames": 96000
  }]
}
```

`state` is `recording`, `completed`, or `interrupted`; `problem` is an optional string.
`startOffsetMs` is omitted until a source receives samples. `frames` includes inserted
silence, is per-channel, and is updated periodically and at finish. A crash may leave a
`recording` manifest and stale frame count; WAV headers are maintained during recording
so the surviving files remain playable. A 32-bit WAV size limit stops recording explicitly.
Filenames are host-generated safe basenames. Readers must reject path traversal/symlinks
escaping the session directory. Mac's browser host saves to `~/Music/Cassini`.

A GNOME host can adopt this folder format to exchange raw sessions, or initially implement
only the wire protocol. No transcript or final Cassini Opus export is implied by this format.

## Tested paths

- Swift tests: timestamp offsets/gaps, channel separation, input validation, sequence errors,
  framing, stop and browser-disconnect finalization.
- `BrowserExtension/tests/worklet.test.cjs`: sample clipping, stereo interleaving, silence,
  render timestamps, partial-tail flush.
- `synthetic-capture.mjs`: actual local WebRTC connection, generated tone, local/remote capture,
  mute to silence, AudioWorklet, actual Swift host and WAV validation. No microphone access.
- `extension-capture.mjs`: loaded Chromium extension, MAIN-world and isolated-world bridge,
  service worker, Chrome Native Messaging and real WAV output from a generated media stream.
  It uses a temporary browser profile and simulates activeTab metadata granted by invocation.

Tests require `swift build` and, for the two optional `.mjs` tests, Playwright/Chromium.
`PLAYWRIGHT_MODULE` may point to a temporary installation's `playwright/index.mjs`.
The integration tests remove only their generated session folders and temporary profile.
They default to the Mac debug host path. Set `CASSINI_NATIVE_HOST` to an absolute executable
path to test a Linux/Python/GStreamer host implementing the same protocol and session layout.
