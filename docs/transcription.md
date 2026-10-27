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
Python interpreter, alongside PyGObject/GStreamer for local Opus decoding. Ubuntu
26.04's python3-numpy and python3-onnxruntime packages (ORT 1.23.2) pass both exact
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
boundary and every word text/startMs/endMs; FIR error is checked against 1e-6.
The GTK proof covers a growing transcript, source captions, separate remote
speakers, offsets, explicit received transcription, unchanged capture hashes,
child reuse, and real child death followed by Try again.

The `whistle-linux-x86_64` CI artifact includes the golden, runtime and UI reports.
The ordinary suite separately covers FIFO queued state, source-byte preservation,
metadata edits during recognition, failed/newer/unsafe metadata, vote/cap/FIR edge
conditions and broker-lag prevention. Recording integration continues to validate
actual private Pulse sources and source-separation thresholds.
