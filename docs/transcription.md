# Bundled meeting transcription (UX13)

Cassini queues a native or browser meeting after its capture has stopped. One
FIFO and one persistent child serve the app. Existing queued/running work resumes
when the Library is reopened. Historical recordings with no transcript wait for
**Write it down**. Received audio also requires that action; it appears in the
Library preview and Meeting header when the meeting can be written down.

The child plans and recognises each track separately. It uses the shared
`cassini-fir-48k-16k-v1` coefficients, float64 ascending-tap accumulation and final
float32 rounding. Stereo averages to mono first. 16 kHz passes through; other
rates are rejected for now. Seekable one-second blocks retain FIR context, and
recognition holds at most one bounded segment in memory.

SpeechGate follows cassini-live's 512-sample Silero frames, 64-sample recurrent
context, 2-of-3 start vote, 0.5/0.35 hysteresis, 320 ms preroll, quiet termination
and 15-second cap with pause preference. State resets per track. Whistle uses
language=None and word_timestamps=true. Word positions include the segment and
track offsets. Unknown track offsets use zero with a diagnostic (D11.2); capture
metadata is never rewritten to invent alignment.

Microphone/local words are `me` (**Me**), native system words are `others`
(**The others**), with provenance `source`. Browser remote tracks remain separate
unnamed people, `track-<trackId>`. First speaking determines their Speaker n
numbers. A received Opus without track identities uses one unnamed speaker,
`track-original`. Equal-time words retain session track order.

Only app-owned `transcript.json` and `meeting.json` are updated. Writes are atomic,
with queued/running/done/failed status, measured ETA and growing timed items.
Existing unknown metadata, names, notes and titles are retained. A worker failure
produces the catalog's transcribe-failed line and **Try again**. It preserves all
capture audio/session bytes, closes the failed child and starts a replacement on
retry. Capture completion is checked independently of broker polling, so a new
live folder cannot be transcribed prematurely while activity reporting catches
up.

## Packaging handoff

The coordinator owns existing packaging manifests; this change does not edit
them. `whistle/fetch.py` and the separate `whistle/meson.build` prepare/install the
bundle. Main Meson installs the app's queue, worker and pipeline Python modules.

In addition to the pinned D5 assets, the bundle now contains **Silero VAD** from
snakers4/silero-vad revision `7e30209a3e901f9842f81b225f3e93d8199902b1`, SHA256
`1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3`, and its
MIT notice. It comes from the pinned upstream URL at build time; it never uses
dist or downloads at runtime. Add this immutable artifact to each package's build
sources/cache and ship LICENSE-Silero-MIT.txt alongside the existing notices.

Runtime dependencies now include **NumPy and ONNX Runtime CPU** for the app's
Python interpreter, alongside PyGObject/GStreamer for child-process Opus decoding. Ubuntu
26.04's python3-numpy and python3-onnxruntime packages (ORT 1.23.2) pass both D11
goldens; cassini-live's ORT 1.31.0 passes too. Other package formats need their
equivalents, or pinned build-time vendoring where a system package is unavailable.
ORT uses one intra-op and one inter-op thread and disables telemetry before
import. The engine remains confined to the child; GTK imports neither Needle nor
ORT. The spike's glibc x86_64 engine limitation remains until another pinned
platform engine is independently validated.

Install the separate bundle into the same prefix/staging root as the app, under
`share/cassini-viewer/whistle`. Developers/tests can explicitly point
CASSINI_WHISTLE_BUNDLE at that directory. The separate Meson project installs
matching worker/pipeline modules too; use the same checkout revision for both.
Without the bundle or runtime dependencies, writing down fails safely with Try
again; audio remains usable. Packaging integration is required for first-launch
offline operation in released packages.

## Evidence and tests

`tests/whistle/run.sh` uses a read-only networkless container with only loopback,
no host display/audio sockets or devices, and private Xvfb/D-Bus for GTK checks.
It exercises the native privacy constructor guard, both immutable D11 goldens,
and the production GTK queue with real speech audio. Goldens assert every segment
boundary, word count and every word’s text exactly. Under D11.3, each word
startMs/endMs may differ by at most 80 ms, only when SHA256 hashes of the float32
samples actually passed to Whistle match the authoritative reference segment
samples. The report retains every timing difference and both hash lists; a hash
mismatch fails even when words match. FIR error is checked against 1e-6.
The GTK proof covers a growing transcript, source captions, separate remote
speakers, offsets, explicit received transcription, unchanged capture hashes,
child reuse, and real child death followed by Try again.

The `whistle-linux-x86_64` CI artifact includes the golden, runtime and UI reports.
The ordinary suite separately covers FIFO queued state, source-byte preservation,
metadata edits during recognition, failed/newer/unsafe metadata, vote/cap/FIR edge
conditions and broker-lag prevention. Recording integration continues to validate
actual private Pulse sources and source-separation thresholds.

## Closing Cassini

Closing the last window or quitting cancels inference without changing a running
job into a failed job. Cancellation signals the child immediately; its IO thread
owns pipe/selector cleanup and reaping. Both queue executors shut down, completion
callbacks are balanced once, and app holds are released. Queued/running work and
already published partial words remain available for the next launch. Re-queuing
does not clear an existing sidecar.

Stop and keep it during close/quit waits for the capture's finished signal and
final checkpoint before closing the windows. Saved audio is marked queued for
the next launch instead of starting hidden inference. Keep recording remains the
explicit choice for background capture. Stop during asynchronous preparation
never transitions the audio pipeline to PLAYING. If capture finishes while the
confirmation remains open, Stop and keep it uses that finalized result and does
not wait for an already-delivered finished signal.

## Received audio streams

Received Opus is decoded in the inference child through a back-pressured appsink
(two buffers). PCM blocks hold at most one second; stereo averages in float32
before the same centred FIR used for captures. Dithering/noise shaping are off so
repeated passes feed identical samples to Whistle. The first sequential pass plans
speech; the second sequential pass skips quiet spans and retains at most one
bounded speech segment. No whole-file WAV or other expanded temporary file is
created, so neither a RAM-backed /tmp nor a RIFF 32-bit size limit bounds meetings.
The decoder has a no-progress timeout, not an overall meeting-duration timeout.

The offline CI proof streams a real two-hour stereo Opus in a read-only container
with /tmp limited to 64 MiB, checks sample counts and block size, measures peak
RSS, and verifies selected segment samples across both passes. The normal real
GTK proof also transcribes received speech, preserving the original Opus bytes.

## Library write boundary and source identity

Only direct canonical meeting folders in the configured Library can be queued
for writing down. The queue checks that boundary before publishing anything and
again before marking a preparation failure. Opening an outside recording remains
read-only; opening a Library file through Open/CLI uses its watched Library model
so progress keeps updating. A damaged embedded body stays on the reading path
with its audio available.

Capture IDs/kinds travel from the scanner's validated/recovered track model to
the worker; recognition never reparses raw session.json. Duplicate/invalid IDs get
separate stable recovery identities without dropping their audio, and worker RPC
IDs use distinct audio filenames. Source labels are assigned only to known mic
and system sources. Mixed/unknown browser streams stay unnamed. Progress rereads
app-owned speaker metadata and preserves user/recognised names changed while the
job was queued or running, including unknown speaker fields.

## Bounded publication and reading work

The first nonempty segment is published promptly. Later growing snapshots are
at least one second apart and require at least 12.5% more words (minimum 32),
with a final snapshot on completion or cancellation. This bounds cumulative
full-sidecar writes to roughly ten times the final word count rather than one
whole transcript per segment. Recomputed resumed work does not replace a longer
partial transcript until it catches up; a complete result can replace it.
Metadata is rewritten only when ETA, speaker data or status actually changes.

The Library preview bounds the legacy aside/overlap grouper to 32 speaker
fragments, preserving long monologues, and caches the result. The separate timed
reading model retains every word. Preview labels show at most three wrapped lines
per excerpt. The Meeting list splices changed paragraphs into stable existing
rows, follows position during playback/explicit seeks, and leaves a paused
reader's visible row in place. Speaker colours rebind on a live theme change;
decorative meters are hidden from accessibility and transport icons are named.

Sequential received-audio rereads also send heartbeats while skipping long quiet
spans, so useful decode progress cannot look like a silent worker timeout.
