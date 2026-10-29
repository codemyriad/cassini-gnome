# S-9 Saved voices conformance (D25)

GNOME authors this snapshot; Mac independently verifies it. It adds no voice
store, production UI, model weights, speaker embeddings or media to this repo.
Android is read only. Its exact reference commit is
`7068c5889c0a70dbb3f01742fc0963f24bfd299a`.

## Logic vectors

`vectors.json` ports all 38 specified Android tests: VoiceMatcher (10),
VoiceprintSelection (7), VoiceCodec (3), NameMatch (14), VoiceStaleness (4).
Some tests make multiple assertions, so there are 65 ordered checks. Their
expectations were written by hand from those assertions and source rules,
then checked against the unchanged Kotlin algorithms. Source paths and SHA256s
are recorded in `provenance`. The 65 actual outputs are independent of these
expected values.

Vectors may be literal float arrays, or compact descriptors:

- `{"axis": i, "dim": n}`: an n-vector with 1 at i and 0 elsewhere.
- `{"near": s, "other": i, "dim": n}`: index0=s, indexi=sqrt(1-s*s), others0.
- `{"normalise": [values]}`: L2-normalise that array.
- `NaN`, `Infinity`, `-Infinity` strings denote non-finite float inputs.

Android uses IEEE754 float32 vector components. Arithmetic outputs have an
absolute tolerance of 1e-6. IDs, order, windows, states and boolean/null values
are exact. `match` outputs include only voiceId/state because the source tests
assert assignment/state; they do not assert scores. Map insertion order is the
source order for stable greedy ties. List order is always significant.

Each case has ordered `checks`. Run the operation on `input`; return its result
in that same position. `select` returns speaker→[[startSample,endSample],…].
It uses consecutive word runs, dominant-overlap native speaker, overlap cuts,
200ms trim, 1500ms minimum, equal integer division into <=10000ms windows,
thirds round-robin longest-first selection, 45s budget, 4s minimum and final
sample clamping, exactly as Android. The cap case keeps 43600ms because its
remaining 1400ms is under the minimum. The long-run case pins its three exact
integer splits in addition to the original count/limit/endpoint assertions.

`voiceRoundtrip` means enrol one fresh voice, encode/decode version1 and return
the declared fields (replace the generated UUID with idPrefix after checking
it). `decodeVersion` reports the unsupported version number. `enrolUnenrol`
enrols first and second unit inputs at one second each, then removes undo;
return mean/count/seconds, or null for a dimension mismatch. A person's seconds
remain 2 after unenrolment. `rank` returns id/score/stale/likely rows.
`dimension` reads the configured pin, not a value from the input.

```sh
python3 verify.py --vectors /path/to/actual-logic.json
```

`actual-logic.json` is an object: each case id maps to an array of its 1+ actual
check outputs. The checker compares all cases, with no missing/extra outputs.
`AndroidOracle.kt` is a small adapter to the original Android code, not a port
of its algorithms. It also emits independently calculated model windows.

## Public S-11 model check

Build the same `../diarization/two-speaker.wav` using its builder and immutable
LibriSpeech pins. `golden.json` pins the S-11 JSON/WAV/PCM and the exact dist
CAM++ model, Apache-2.0. Keep dist's actual licence/notice with the downloaded
weights. No weights or embeddings are exchanged by this snapshot.

For each `d25-full` print, take the two windows selected by unchanged Android
from the S-11 frozen words and turns. Feed every window into one online stream
at16kHz, in the listed order, then InputFinished, IsReady, ComputeEmbedding.
Require512 values and L2-normalise. The SHA256 is of the concatenated input,
mono little-endian float32 PCM16/32768, without gaps/padding between windows.
The other four prints use individual selected windows from those same full
prints, each with a fresh stream. They are controls, not a different product
selection rule. S-11 repeats each reader's original utterance, so these are
not independent recordings or a broad accuracy calibration.

Frozen results: full inter-speaker cosine0.504644; same speaker across windows
0.714607/0.753138; different speakers across windows0.168565–0.483535.
For both matching scenarios, enrol each reader's first window as one saved
voice. The full prints match AUTO/AUTO. The second windows match
SUGGESTED/AUTO:0.714607 is below auto0.72. Thresholds stay auto0.72,
suggest0.50, margin0.08. These outcomes were independently checked by the
unchanged Android matcher on the actual native prints. No thresholds or
windows were tuned after observing scores.

Three fresh offline fork C-API workers gave byte-identical vectors. The fork
is v1.13.7-cassini.8, CPU/two threads, ORT1.27.1. Config is zeroed then model,
num_threads2, providercpu; dim512. The broad earlier throwaway turn-based spike
had unstable long-utterance comparisons; this golden uses D25 word runs and
edge trims instead. The results above do not replace those spike observations.

Both apps must reproduce all15 cosines within ±0.02, exact input hashes,
selection indices,512 dimensions and exact match outcomes. Do not adjust the
golden if another platform differs; report the evidence to the coordinator.

```sh
python3 verify.py --audio /local/two-speaker.wav --actual /local/model-report.json
```

Model report shape (supply all6 prints,15 comparisons and both match maps):

```json
{"inputSHA256":"<WAV hash>","modelSourceSHA256":"<model revision>","dimension":512,
 "prints":[{"id":"others~1/d25-full","windows":[[12160,160640],[344944,469632]],
 "inputSHA256":"<float32 input hash>","norm":1.0}],
 "comparisons":[{"a":"<print id>","b":"<print id>","cosine":0.5}],
 "matches":{"full":{"others~1":{"voiceId":"voice_reader1","state":"auto"}},
 "secondWindows":{"others~1":{"voiceId":"voice_reader1","state":"suggested"}}}}
```

Running `verify.py` without arguments checks snapshot hashes and dimensions.
Names/voiceprints from real meetings must never enter these files. S-11's
attribution and licence continue to apply to its public input. UI, crash-safe
store/generation races and never-export behavior belong in app tests (UX17).

## Desktop clarifications (coordinator, 2026-10-29)

`desktop-vectors.json` adds two cases without changing any of the38 Android
vectors or the model golden. `selectResolved` resolves UX16 merges first and
regards every merged native label as the same person's own speech. It cuts
only genuine other labels. Its example keeps0–9s and10–13s before edge trims,
so the exact selected windows are0.2–8.8s and10.2–12.8s (11200ms total).
The former dominant-label Android behavior would discard the merged voice's
second part; the coordinator explicitly chose to retain it on desktop.

`meetingKeys` uses the full Recording-/Received- folder basename for private
meeting print files and profile meeting lists. The same UUID under the two
prefixes denotes two meetings under Library D6.1. Per-meeting files still use
D25 desktop version1 with speaker-ID keys; Android's version2 variant keys
are not adopted. These choices were confirmed through R28/R29.

`verify.py --desktop actual-desktop.json` compares an object mapping each
desktop case id to its expected-shaped independently calculated output.
