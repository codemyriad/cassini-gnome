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

G14: the voice step also keeps at most one group's scratch file, 64 KB per second
of meeting, whatever the number of sources or spans. It rejoins each group to pick
windows, then rejoins again only the groups whose windows a print reads, and
refuses audio that changed between the two passes. A print's audio, at most 45 s
of float32 (2.9 MB), is embedded and freed as soon as all its windows are read.
Only a speaker merged across groups keeps its own audio until its last group;
prints are still sent in their original order, each finished one waiting as a
512-value vector. The voice pass checks for live recordings when it starts, not
when writing down started.

Before every scratch file, diarization and the voice step require free space for
that file plus a 1 GiB floor. When the app has a recording running on the same
volume as the job starts, the recorder's reserve, max(1 GiB, 2 %), applies
instead. A recording that starts mid-job is covered only by the floor and by the
recorder's own low-disk handling; that gap is accepted. Without the space nothing
is written and the transcript is kept:

- Diarization: that group keeps D11. When no group could be separated and every
  skip was for disk space (groups shorter than 0.2 s aside), `speakerSeparation`
  records `skipped` with reason `lowDisk` (D56); when other groups were separated
  it is `partial`, with reason `lowDisk` unless a group also failed for another
  reason (`processingFailed`). The numbers go to the app log.
- Voice step: the whole step fails, as a full disk did before, so saved voices,
  matches and rejections stay as they were. The numbers go to the app log; the
  file records nothing (D56).

D56: tracks with fewer than 32 non-zero 32 ms frames (see `docs/transcription.md`,
Silent tracks) are left out of every group's rejoin, in diarization and in the
voice step. Eligibility is still decided on every track, so a silent remote never
makes the microphone the only group. A group of only silent tracks is absent;
when no eligible group is left, `speakerSeparation` is `skipped`,
`notApplicable`.

The pure word assignment follows the shared S-11 vectors. Stable IDs use the
original group followed by `~n`; display numbering follows speaking word order.
If fewer than two voices receive words, the D11 speaker is retained. A model
error keeps the recognised words. A child crash after the recognition-complete
marker also finishes with D11 speakers instead of failing that transcript.

`speaker-turns.json` is atomic app-owned cache data and is not exported. Its
optional per-group `inputSHA256` is the hash of rejoined float32 PCM. A retry can
reuse turns only when the model, parameters, duration and actual input hash
match. Words are reassigned once after recognition, with their text, timing and
other fields preserved.

A `<group>~n` id is only a rank of first turns, so a name is tied to the input
that produced it (G12). Each diarized speaker entry in `meeting.json` carries
`voiceSource: {inputSHA256, turnsSHA256}`: the group's input hash and the
SHA-256 of the group's usable turns as canonical JSON `[startMs, endMs,
ordinal]` rows in turn order. On a rerun, a typed, recognised or cleared name
stays on its id only when the stamp is equal, as it is for reused turns.
Otherwise the entry becomes unnamed with the new stamp, as does any other
`<group>~*` entry of a re-diarized group, and merges from or into those ids are
removed in the same write. A dropped name is only logged (`voiceNameReset`).
Entries without a stamp count as different; D11 ids such as `me`, `others` and
`track-<id>` are unchanged. Renames and merges keep the stamp. The private voice
cache carries a confirmed match, its print and rejections only for an equal
stamp. Remember, preparation checks and Merge/Undo or background recomputation
treat a cached print whose stamp differs from the same id in `meeting.json` as
absent, and recomputed prints take their stamp from `meeting.json`. Voice
preparation labels saved ids from turns only for an equal stamp. The field name and shape are provisional until coordinator decision R74.

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
