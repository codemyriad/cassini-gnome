# S-11 diarization conformance (D24)

GNOME authored this snapshot; Mac independently checks it with its pinned
sherpa C API / ONNX Runtime 1.28.1. Model inference is offline. These files do
not change the browser protocol, resampler, VAD or recognition goldens.

## Assignment vectors

`assign-vectors.json` contains 22 hand-authored cases. Its provenance pins
gocassini's `cassini-go-recorder/internal/transcribe/diarize_test.go` at
`e5746ec256b243fe5c3154b98c528d2fff32a3f0`. The seven assignment tests include
both no-turn and invalid-turn variants. They are followed by normalization,
shared-stream splitting, deterministic output and turn-based numbering cases,
the D24 float32/inconclusive/sourceTrackId additions, and group/rejoin policy.
All 17 assignment/normalize/split/apply cases were independently checked
against the unchanged Go functions; the five grouping/rejoin cases express D24.

Operations:

- `assign`: preserve word input order. Return native labels and candidate voice
  IDs ranked by each native speaker's first valid turn, not first assigned word.
  `null` means no valid turns. Empty/invalid turns leave product words unchanged.
- `normalize`: first coerce each finite seconds value to IEEE754 float32,
  promote it to double, multiply by 1000 and truncate toward zero. Clamp both
  ends to `[0,durationMs]`; remove invalid/empty/negative-speaker turns and sort
  `(startMs,endMs,speaker)`. The strings `NaN`, `Infinity`, `-Infinity` represent
  non-finite native values; they are strings to keep the JSON strict.
- `split`: the pure Go split helper, preserving other speakers, text-only
  legacy segments and arbitrary word attribution fields. Rebuild in word-time
  order. `subSpeakerIds` follows turn ordinal; a voice without words is omitted.
- `applyGroup`: candidate IDs above, followed by D24's inconclusive-group rule.
  With fewer than two candidate voices receiving words, effective word IDs and
  voice IDs stay at the original D11 group. The single-word rerun keeps its raw
  candidate `p~2`, while its effective product ID is `p`. This intentionally
  distinguishes stable raw numbering from the D24 fallback, rather than changing
  the pinned Go helper's numbering test.
- `groupTracks`: preserve first group/track occurrence. D11 `me` and `others`
  identities take precedence; browser `sourceTrackId` joins off/on spans and
  labels never establish identity. Remote participant groups are ineligible;
  native system/whole-tab/received groups, and a microphone-only room, are eligible.
- `rejoin`: its tiny already-16kHz PCM inputs exercise leading silence and sum.
  Values are s16 divided by 32768 before summing, without clipping. Inputs
  shorter than 3200 samples never invoke the model. The common FIR has its own
  unchanged D11 goldens.

Assignment unions each speaker's overlapping/touching intervals, sums overlap
per speaker, and takes the first speaker encountered on a tie. With no positive
overlap, choose the nearest union turn, with ties in sorted turn order. Display
names/colours follow first speaking word across the meeting (R7); they do not
determine stable voice IDs. Optional display expectations cover that distinction.

## Natural two-speaker input

```sh
python3 build.py --cache /path/to/fixture-cache --output /path/to/two-speaker.wav
# Afterwards, the same build is possible without network:
python3 build.py --offline --cache /path/to/fixture-cache --output /path/to/two-speaker.wav
```

The builder uses Python's standard library only. It verifies immutable WAV
source pins and the final PCM/WAV hashes. It requires no model package, codec,
subprocess, audio device or display. It writes no model weights or source media
to a public repository.

Two LibriSpeech test-clean readers alternate A/B/A/B. Placements are exact
sample indices: A `[0,166960)`, B `[174960,341920)`, A `[309920,476880)`,
B `[484880,651840)`. Thus there is exactly one 32000-sample / two-second overlap
and two half-second gaps, for 40.740 seconds total. Each source is multiplied
by 1/2, truncating toward zero, before integer summing; the mix does not clip.
The two utterances are from different speakers and share no gocassini audio.

`fetch-pins.json` pins the prepared WAV mirror by repository commit, byte size
and SHA256. Its PCM was independently compared byte for byte with FLAC decoded
from the official OpenSLR test-clean archive; original member names, FLAC/PCM
hashes and the archive SHA256 are recorded. The normal builder fetches only the
two small WAVs, not the full archive. See `ATTRIBUTION.md` for origin and licence.

## Frozen native golden

`golden.json` freezes the first int8 run with D24's exact zeroed C config plus
model path, CPU/2 threads and min-duration 0.3/0.5. The other two fresh children
gave identical raw turns. The C API reports 16kHz and two speakers. No native
labels, thresholds or timings were tuned after observing another platform.

Whistle's existing D11 pipeline produced the 113 frozen word spans, with its
pinned Needle/Whistle/Silero/Python-ORT 1.31.0 and automatic language. Those
words are inputs to this diarization-assignment conformance test; S-11 does not
replace the existing separate recognition-text/segment goldens. Normalized
turns and word voice IDs were computed by unchanged pinned gocassini functions.

Mac processes the identical generated input using its approved int8 model and
fork. It must report two voices, assign the same voice to at least 98% of these
frozen words, and place corresponding turn boundaries within 160ms. Normalize
its raw float32 times by D24 before comparison. Stable voice ordinals come from
first turns, so arbitrary native labels are not compared across platforms.
Report differences, including turn-count differences, instead of editing the
golden or tuning the model. The S-11 tolerances do not relax the exact assignment
vectors. Nemotron weights are acquired from dist separately and never committed.

Golden report format for `python3 verify.py --actual report.json`:

```json
{"inputSHA256":"<golden input sha256>","modelSourceSHA256":"<47c221ea… full revision>",
 "speakerCount":2,"turns":[{"speakerId":"others~1","startMs":349,"endMs":10130}],
 "wordSpeakerIds":["others~1"]}
```

Supply all actual turns and all113 independently derived word assignments.
The example above shows the shape only. The checker pairs turns per stable
voice, requires integer D24 milliseconds, and reports the word mismatch indices
and largest boundary delta. `--vectors outputs.json` compares each case id's
expected-shaped output exactly. `verify.py` without arguments verifies hashes
and declared fixture dimensions. `SHA256SUMS` pins the shared snapshot files.
