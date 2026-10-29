# DC-robust speech detection (D26.1)

Silero VAD has no DC handling of its own. Neither do its reference utilities or sherpa-onnx's wrapper, which
pass raw samples into the model. It relies on the capture chain having removed DC already. In browsers, and in
PipeWire/PulseAudio echo-cancel, that is WebRTC APM's capture high-pass. Native capture of a mic with an ADC
offset skips that step, so Silero sees a large DC bin and misses quiet speech.

## The filter: WebRTC APM's capture high-pass, applied to the VAD input only
2nd-order Butterworth high-pass, 100 Hz: `[B,A] = butter(2,100/8000,'high')`. This is the design in freedesktop
webrtc-audio-processing `high_pass_filter.cc`, linked by PipeWire and PulseAudio echo-cancel, and in older upstream
WebRTC. The classic WebRTC VAD applies the same kind of filter (a double zero at DC, "Remove 0 Hz - 80 Hz") in
front of its features.

The coefficients are exact binary64 literals at 16 kHz. Never recompute them at runtime. b0+b1+b2 == 0.0 exactly,
which gives a true double zero at DC.

    b0 =  0.9726138984998437
    b1 = -1.9452277969996874
    b2 =  0.9726138984998437
    a1 = -1.9444776577670935
    a2 =  0.9459779362322813

Reference loop, Direct Form I. Use binary64 state and arithmetic, evaluate left to right, no FMA, no vDSP biquad:

    y = b0*x + b1*x1 + b2*x2 - a1*y1 - a2*y2
    if -1e-25 < y < 1e-25: y = 0.0      # denormal guard, as in libopus dc_reject
    x2 = x1; x1 = x; y2 = y1; y1 = y
    out = float32(y)                    # round only when handing the frame to Silero

State:
- One filter per VAD instance, i.e. per track per job.
- Zeroed on VAD reset, as WebRTC zeroes it on Reset.
- Carried across frames, chunks and recognition windows for the whole track. Never reset at segment boundaries.
- Filter each 512-sample frame BEFORE it joins Silero's 64-sample context.
- No priming: zero-state steps of up to ±16000 LSB never produced a segment.

Scope:
- Filter ONLY the samples Silero sees.
- Leave untouched: the recogniser input (Whistle, Parakeet), the diarizer, voiceprint windows, the stored WAVs, and
  the D11 resampled golden and D11.3 hashes. Whisper and NeMo front ends need no waveform DC step.
- Version the stage as `silero-input-hpf-butter2-100hz-v1`.

## Shared vector `dc-quiet-jfk-v1`
Source: the D11 JFK input `tests/whistle/pipeline/pipeline-input-48k.wav` (sha256 b7062e41…3963).
Transform: `y = ((x + 64) >> 7) - 2060` (arithmetic shift, no clipping). The result is 48 kHz mono PCM16 with
AC RMS of about 34 LSB and DC of −2060, the regime of the real failing mic. Use these committed files; don't
regenerate them, because WAV headers differ between writers.

| file | sha256 |
| --- | --- |
| dc-quiet-jfk-v1.wav | 071f051241d394587c85463272e9414f133ee18c4bb835d1382b662e4154efda |
| dc-quiet-jfk-v1-control.wav (no −2060) | e31ed958621a39baf1d33dab6d10798695b8319e78fcaa1cb672cdb43c5f7830 |

The pipeline is the D11 FIR to 16 kHz, then the VAD with the filter, then the gate at 0.5/0.35 with 2-of-3 votes.
The asserts apply to GNOME and Mac, which share D11. iOS and Android use other resamplers and thresholds; see below.

| # | Assert |
| --- | --- |
| a | Filter off: 0 segments, max probability < 0.1 (measured 0.077). |
| b | Filter on: segments exactly `[startSample, endSample, reason]`: [105984,189440,silence] [347648,367616,silence] [371200,413184,silence] [416256,458240,silence] [504320,544256,silence] [547328,589312,silence] |
| c | The control file with the filter on gives identical segments (DC invariance). |
| d | Filtered VAD-input float32 LE sha256 (604672 samples) = eeaeee543bc328acf6f0c4557e8aba86652e0144d9763264e1e5d62a3d13f968. The first three samples are −0.039744045585393906, −0.06441963464021683, −0.05248776078224182. The control's hash is 6ece4c5cacfc2456cb79d0aa1b16f4aa3912fef6697912e633886d7b0da545ab. |
| e | Soft: 251 of 1181 frames at or above 0.5 (±2); max 0.718 ± 0.01. |

If (d) fails on Swift while (b) passes, report it; the contract may relax to segment equality.

Golden change: the JFK pipeline-golden.json segment 1 startSample moves from 14336 to 13824 (−32 ms); the other 7
segments are unchanged. GNOME and Mac land this bump together.

## Level meters and the silent-mic check
- Measure the AC level per 100 ms window: `ac_rms = sqrt(max(0, mean(x²) − mean(x)²))`. On the Mac, the peak is
  `max|x − mean|`.
- Thresholds stay as they are. Never filter the WAV.
- A mic carrying only DC must count as silent. Optionally log the window mean as a "DC offset" diagnostic.

## Per app
- **GNOME:** `speech_pipeline.SpeechDetector`.
  - `reset()` zeroes the four state values.
  - `probability()` filters the frame and joins the FILTERED frame with the context.
  - Remove any DC blocker from `audio_chunks()`.
  - Meter: compute from the appsink buffers (native_recorder/native_tap, recording_details), not the GStreamer
    `level` element.
- **Mac:** `SpeechPipeline.swift` `SpeechDetector`: add the state in `reset()` and filter in `probability()` before
  `cassini_vad_probability`. `mono.f32` stays raw. Meter: `RecordingSink.swift` levels.
- **iOS:** the VAD threshold is 0.18, and the VAD runs through sherpa's wrapper (SpeechFeed).
  - Put the same filter, with state carried across the 28 s windows, on the samples fed to the VAD only.
  - WordGate measures raw sample magnitude, so a DC as small as 0.0005 counts every sample as active. Give it AC
    measures (mean removed per word window) or the filtered samples.
  - Meter and waveform: AC RMS.
  - Assert "0 segments without the filter becomes >0 with it" on the vector; the exact segments don't apply.
- **Android:** sherpa Vad (threshold 0.18) in IncrementalRecognizer windows.
  - Same filter, with the state held outside a per-window Vad.
  - Its own Opus captures are already DC-free (libopus dc_reject), but AAC captures (API 26–28) and imported files
    are not.
  - Meter: OpusCapture subtracts the frame mean.

## Not done, and why
| Option | Why not |
| --- | --- |
| One-pole DC blocker (R=0.995) | Weaker under 1 Hz drift (640 ms late). |
| Kaldi per-frame mean | False segment under drift. |
| Global mean | Offline only; fails under drift. |
| Pre-emphasis 0.97 | Breaks Silero even on clean audio. |
| Lower threshold | No precedent. |
| WebRTC main's 3-biquad | Also works, but its zeros are not at DC: DC and no-DC runs differ by 32–64 ms at two boundaries, and it is 3× the cross-platform surface. |

Filtering the recogniser path is a separate, golden-breaking decision. Revisit it only if recognition on such mics
turns out poor.
