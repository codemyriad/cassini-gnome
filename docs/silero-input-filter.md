# DC-robust speech detection

D26.1 (`tests/dc/README.md`, coordinator commit `196c179`) applies
`silero-input-hpf-butter2-100hz-v1` only inside `SpeechDetector.probability`.
It is the WebRTC APM capture high-pass: fixed binary64 coefficients, Direct
Form I, left-to-right arithmetic, zero initial state and a 1e-25 denormal guard.
Reset clears its four state values per track; segment boundaries do not reset
them. Silero's 64-sample context contains the filtered samples.

The SpeechGate buffers, recogniser input, diarization, voiceprint windows,
FIR reference and recorded WAVs are unchanged. Only the first JFK segment's
start moves from 14336 to 13824; its recognition input therefore includes 512
additional original samples. Shared goldens from coordinator `7e7000f` keep all
word text/counts, model pins, other segment boundaries and existing text
exception scopes. The first phrase's regenerated word times move at most 48 ms.

Native recorder and browser microphone taps calculate AC RMS in 100 ms windows
from appsink PCM, carrying partial windows across buffers. The read-only
recording observer uses per-channel AC RMS too. Meter thresholds stay unchanged;
the GStreamer `level` element is not used. Capture PCM is never filtered or
rewritten for a meter.

A completed transcript with no words and nonempty audio shows the catalog's
no-speech info line in Meeting and the Library preview. Its action re-queues
the normal write-down job with current models. Missing, damaged, failed and
zero-duration audio retain their separate states.

Validation:

- `tests/test_silero_input.py`: filtered context, per-track reset, raw-frame
  immutability, denormal guard and version.
- `tests/dc/probe.py`: coordinator-owned DC/control WAVs, all asserts a-e with
  actual pinned Silero, including exact input SHA256 and the failing unfiltered
  baseline. It runs inside the existing network-disabled offline container.
- Recording callback tests prove DC-only PCM is silent and saved byte-for-byte;
  meter tests cover 100 ms windows split across callbacks.
- Queue and private GTK tests exercise empty-result visibility and normal retry.
- `tests/dc/regenerate.py` documents the shared-golden regeneration procedure;
  existing pinned Whistle/Parakeet checkers and package smoke tests validate the
  adopted bytes with their original strictness.
