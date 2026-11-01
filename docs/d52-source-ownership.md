# D52 audio placement at source changes

The S-6 selection and othersSource events assign half-open time windows to
microphone/local, system and browser (remote/media/tab/application) tracks.
A window owns its start sample and excludes its end sample. A source does not
own audio while its side is switched off. With no applicable recorded events,
ownership stays unknown and the original audio path is unchanged.

The scanner derives immutable windows for validated capture tracks. Playback
and export mask bounded PCM on their meeting timeline. Export masks again after
resampling, so a resampler cannot leak unowned samples beyond a boundary. The
speech child uses the same windows at 16 kHz in both VAD and segment rereading;
diarization and voice extraction rejoin that same PCM. Samples outside a window
are absent from those inputs, and the stored WAV/Opus tracks remain untouched.

Window edges use exact rational sample placement, with ceil at both ends. An
unknown start offset stays unknown, emits an ownership diagnostic and is never
used as an invented zero for clipping. The existing D11 diagnostic/word-time
policy for an unknown offset is unchanged.

Tests run both literal shared ownership vectors through actual WAV export,
recognition reads and diarization input, with a real folder seek. The two reader
regressions fail on the preceding #143 head. They check a physical two-second
overlap, exact zeros outside ownership, unchanged input files and no doubled
sample at the seam. Microphone/local and application kinds are covered.

This is the reader portion of G8. Initial native-only stand-in adoption still
needs the R67 transport binding, and the portable source report/provenance is in
the stacked #144 proposal. G1 conflict-lane IDs (R64) and R63 UI copy remain
pending shared publication. No shared schema/catalog or library_v2 core is
modified by this step.
