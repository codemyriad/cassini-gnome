# Headless realtime transcription pilot

The GNOME capture writer can hand successfully written PCM to a `LiveAudio`
queue. The queue is optional and belongs to one recording. Pass it when
constructing a `NativeRecorder` or `BrowserCapture` for the pilot, and consume it
off the capture and UI threads. Source IDs refer to capture spans. The frame
index is relative to the track, and the track offset places the span on the
recording clock. Discontinuities reset the decoder window. A source can contain
several speakers.

Start one verified `WhistleClient` or `ParakeetClient` per recognizer session.
`LiveTranscription.accept` produces timestamped replacements for one source as
PCM arrives. Apply them to a consumer-owned `DraftTranscript`. Its snapshot
holds the words and the point through which the preview has settled. Settled
words can still change when the final transcript is published. Audio and the
existing publication pipeline remain the authority for export and recovery.

After the writer stops, `drain` consumes queued audio and settles the remaining
draft. Pass the saved source WAV paths to fill any ranges lost to an overloaded
preview queue. A caller receiving `needs_saved_audio` can use the existing
saved-recording job. This pilot keeps draft state in memory and leaves final
publication, diarization and saved-voice matching with the existing job.

Both model adapters currently use bounded rolling windows. These provide a
working comparison with the existing batch job and a baseline for evaluating
native Whistle partials or a speech-gated Parakeet schedule.

Run from the GNOME checkout with the pinned model assets available:

```sh
PYTHONPATH=src python3 tests/recording/replay-live.py \
  --bundle /path/to/whistle \
  --model /path/to/parakeet-ultra-int8-v2 --runtime /path/to/sherpa \
  --reference tests/whistle/jfk.txt tests/whistle/jfk.wav
```

Omit the Parakeet paths for a Whistle-only run. Pass multiple WAVs for sources
that start together and one reference per WAV to compare live and batch word
error rates. Lower the queue capacity to exercise recovery. The report includes
time to first words, draft revisions, overload and elapsed time. The fixture
checks the integration. Measure representative calls and device budgets before
using it to choose a live decoding policy.
