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

## Packaging

Every released package (Ubuntu 26.04 and Debian testing .debs, Fedora and
openSUSE RPMs, Arch, Flatpak) bundles the engine and writes meetings down
offline on first launch; CONTRIBUTING.md ("Writing down offline, in every
package") describes how each format does it. In short: `build-aux/whistle-cache.sh`
downloads the pinned artifacts once (sha256-checked, with retries); Meson
with `-Dwhistle=enabled` runs `whistle/fetch.py --offline` (no download,
fails closed), `whistle/verify.py`, and installs the bundle under
`share/cassini-talk/whistle`, never stripped. Main Meson always installs the
app's queue, worker and pipeline Python modules. `-Dwhistle` defaults to
disabled, for source builds, Alpine and other architectures.

In addition to the pinned D5 assets, the bundle now contains **Silero VAD** from
snakers4/silero-vad revision `7e30209a3e901f9842f81b225f3e93d8199902b1`, SHA256
`1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3`, and its
MIT notice. It comes from the pinned upstream URL at build time; it never uses
dist or downloads at runtime. Add this immutable artifact to each package's build
sources/cache and ship LICENSE-Silero-MIT.txt alongside the existing notices.

Runtime dependencies now include **NumPy and ONNX Runtime CPU** for the app's
Python, alongside PyGObject/GStreamer for child-process Opus decoding. NumPy is
the distribution's own (the Flatpak installs the pinned cp313 NumPy 2.5.4
wheel). ONNX Runtime is not: every package carries the pinned upstream PyPI
wheel of ORT 1.31.0 (cassini-live's version) for its Python's ABI
(`whistle/pins.json` pins cp311 to cp314, with their sha256), because the
distributions' packages cost far more than the engine they serve. Fedora 44's
python3-onnxruntime requires sympy, whose weak dependencies (matplotlib,
scipy, cython, TeX Live) make a default install 485 packages and 1 GiB;
Ubuntu 26.04's pulls in 24 packages and 191 MB (oneDNN, sympy, the shared
library). The PyPI wheel needs only NumPy at run time, and Whistle with its
environment must stay lighter than sherpa-onnx with Parakeet. Measured on a
system that already has the app's GTK, GStreamer and PyGObject, with default
package manager settings, installing the package now adds 5 to 10 packages
and 93 to 166 MB (Ubuntu 26.04 and Debian testing without recommends: 93
and 110 MB; Fedora 44 with weak dependencies: 166 MB, was 487 packages and
1.2 GB; Tumbleweed: 120 MB; Arch: 122 MB, was 483 MB), and the Flatpak
installs in 112 MB instead of 141 MB.

Meson (`-Dwhistle=enabled`) runs `whistle/fetch.py --onnxruntime cpXY
--offline`, which takes the wheel for the Python the launcher runs with from
the same pre-filled cache, unpacks it without `transformers/`,
`quantization/`, `tools/`, `backend/` (model tools and the ONNX backend
adapter, which import sympy, onnx and packaging) and the separate C API
library (the extension module links the runtime statically), about 31 MB, and
`whistle/verify.py --onnxruntime` checks every file against the wheel's
RECORD. It is installed in `<libdir>/cassini-talk/python` (for example
/usr/lib/x86_64-linux-gnu/cassini-talk/python on Debian, /usr/lib64/... on
Fedora and openSUSE, /usr/lib/... on Arch, /app/lib/... in the Flatpak), never
stripped, and Meson writes that path to `cassini_talk/paths.json`. Only the
recognition child puts it first on `sys.path`, after checking it against
RECORD again; the GTK process never does. Without a private copy for the
running Python's ABI (a source run, a build with `-Dwhistle=disabled`), the
child uses the Python's own onnxruntime, if any. The packages depend on
exactly the Python version they were built for (`python(abi)`, `python3 (>=
X.Y~), python3 (<< X.Y+1)`, `python>=X.Y python<X.Y+1`), like any extension
module, and no package tool sees the private dist-info (it is outside
site-packages; the RPM excludes it from dependency generation explicitly), so
nothing depends on sympy, protobuf or flatbuffers. The upstream extension
module also links curl and mbedtls statically for ORT's Azure execution
provider, which the app never uses (it creates only CPU sessions); Debian's
lintian override says so. With no network, both D11 goldens pass exactly (8/8
segments, 68/68 words, 0 ms) with the private ORT 1.31.0 on Ubuntu 26.04,
Debian testing, Fedora 44, Tumbleweed, Arch and in the Flatpak. Alpine's ORT
works but the glibc engine cannot load under musl, so Alpine stays a
source/check target. ORT uses one intra-op and one inter-op thread and
disables telemetry before import. The engine remains confined to the child;
GTK imports neither Needle nor ORT. The spike's glibc x86_64 engine limitation
remains until another pinned platform engine is independently validated.

Developers/tests can explicitly point CASSINI_WHISTLE_BUNDLE at a bundle
directory. Without a usable bundle (not installed, not Linux glibc x86_64) or
without NumPy or an ORT (private or the Python's own), the app hides **Write
it down** and **Try again**, does not queue recordings, and logs "Writing down
is not available on this computer" with the reason
(`transcription.unavailable_reason`, which locates but never imports NumPy/ORT
in GTK); audio remains usable. A user-facing line for this needs a new
strings.json entry approved by the catalogue owner.

Every package build's clean-install step runs the installed worker on both
D11 goldens in a container with `--network none`
(`build-aux/smoke-installed.py --whistle`), so a released package is proven
to write down offline, with its own dependencies, before it is published.
The smoke test also requires the private ORT to match the installed Python
and its RECORD, the child to report the pinned ORT version, and the app's
Python to have no onnxruntime of its own.

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
