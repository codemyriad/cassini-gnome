# Optional speaker diarization (D24)

Tell speakers apart uses `nemotron-3-diarization-int8`, downloaded only on an
explicit request in Settings → Transcription → Speakers. The registry is
`src/cassini_talk/models.json`. Both the manifest and the compressed and raw
files must match its pins. The model's licence is **OpenMDW-1.1**; the actual dist
notice is retained with the revision. The weights are not included in a package.

The existing pinned sherpa runtime must pass a separate child-process capability
check before the row shows On. The feature does not load a native library, NumPy,
or ONNX Runtime in the GTK process. A Whistle-only build remains usable without
sherpa or these optional weights. Removing the model leaves existing transcripts
and speaker names intact.

For new write-down jobs, both speech engines run diarization after recognition
in their speech child. The configuration is zero-initialized except for the
Nemotron model path, CPU provider, two threads and minimum on/off durations of
0.3/0.5 seconds. There is no extra VAD or clustering stage. The C layouts are
checked against the pinned header by the existing compiled ABI test.

D11 groups are preserved. System sound, browser mixed-audio sources, locally
transcribed received audio and a sole microphone group are eligible. Independent
participant tracks are left alone. Audio uses the shared downmix/FIR and s16 to
float32 conversion; spans are summed on the meeting clock, with silent gaps.
An unnamed disk-backed scratch file is mapped by the child rather than decoding
an entire long meeting into RAM or `/tmp`. Inputs shorter than 0.2 seconds do not
construct or call the model.

The pure word assignment follows the shared S-11 vectors. Stable IDs use the
original group followed by `~n`; display numbering follows speaking word order.
If fewer than two voices receive words, the D11 speaker is retained. A model
error keeps the recognised words. A child crash after the recognition-complete
marker also finishes with D11 speakers instead of failing that transcript.

`speaker-turns.json` is atomic app-owned cache data and is not exported. Its
optional per-group `inputSHA256` is the hash of rejoined float32 PCM. A retry can
reuse turns only when the model, parameters, duration and actual input hash
match; typed names are retained by stable ID. Words are reassigned once after
recognition, with their text, timing and other fields preserved.

## Verification

Run `pytest` for the shared 22 assignment/group/rejoin cases, cache invalidation,
model lifecycle, actual child-crash salvage and compiled C layouts. Run Ruff on
`src tests build-aux`; vendored conformance scripts retain their upstream bytes.

The offline workflow prepares the pinned model through the real D4 store and
rebuilds the natural two-reader LibriSpeech input from immutable hashes. It runs
`tests/diarization/run.sh` against installed modules with both Whistle and
Parakeet. The container has no network, a read-only root, two CPUs and 2 GiB RAM.
The shared checker requires two voices, at least 98% frozen-word agreement and
turn boundaries within 160 ms. It also checks cache reuse and the production
queue, including keeping a typed speaker name and capture bytes on retry.

`tests/ui/diarization-models.py`, through `build-aux/ux-screenshot.sh`, drives the
real model store against tiny loopback fixture weights. Only its native
capability check is injected. Both light and dark modes cover Download, Cancel,
failed transfer, Try again, On and Remove. The settings-transcription board shows
On using the existing screenshot fixture mechanism; it never downloads weights.
