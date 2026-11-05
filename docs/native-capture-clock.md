# Native capture clock

The recording diagnostics reproduced the intermittent initial/restart stream
error with a first system chunk at 42,949,672,970 ms (960 bytes, zero frames
previously written). RecordingSession correctly rejected it; the later generic
GStreamer bus error was a consequence. The retained pipeline used
GstPulseSrcClock, with both Pulse sources offering a device clock.

The failing offset equals (2^32 + 1) ten-ms segments. GStreamer's audio-source
skew path advances ring-buffer segment counters against the selected clock;
this is consistent with a startup clock/segment wrap, rather than an inaudible
source or an insufficient sample-count wait. It is an inference from the
retained exception and upstream source, not a proven upstream defect.

Both native WAV capture and the Opus recorder now select GstSystemClock on their
own pipeline and set provide-clock=false on their Pulse sources. The source's
normal clock-slaving behavior remains enabled. This selects one monotonic time
reference for the independent sources; it does not rewrite timestamps, clamp
invalid chunks, change desktop clocks/devices, or relax the writer's validation.

The private comfort proof records the selected clock and compares initial track
offsets with pipeline running time on each of its three capture starts. All
existing readiness, frame-count, duration, tone, bleed, warning and reserve
conditions remain unchanged. The detailed GST/Pulse/appender evidence remains
available if another cause fails capture.

References: [GStreamer clock selection](https://gstreamer.freedesktop.org/documentation/application-development/advanced/clocks.html),
[AudioBaseSrc provide-clock](https://gstreamer.freedesktop.org/documentation/audio/gstaudiobasesrc.html),
and [the upstream 1.28 audio-source skew path](https://github.com/GStreamer/gstreamer/blob/1.28/subprojects/gst-plugins-base/gst-libs/gst/audio/gstaudiobasesrc.c).


Source changes stage a live branch and require its first valid PCM sample before
committing the selection, track endings and new track offsets. The staged
appsink has async=false so its preroll cannot pause the confirmed pipeline.
Readiness buffers are discarded; post-checkpoint buffers keep the shared clock.
Errors from staged or retired branches are attributed to that attempt, allowing
the previous sources to continue and the dialog to show its existing retry line.
The isolated sources harness rejects a nonexistent real Pulse system device,
checks unchanged events/track IDs and continuing microphone samples, then proves
a successful retry and all existing tone, bleed and Stop conditions.

Protocol pin `56814d5` adds native host-time discontinuity accounting. Each
native span compares consecutive input buffer stamps, subtracts the previous
buffer's frame duration, and rounds a positive remainder to the nearest frame.
At least one second becomes silence counted in `gapFrames`. Smaller or backward
skew is diagnostic only: sample counts drive continuity, so smooth device drift
does not accumulate into missing audio. Native browser microphone and fallback
system taps use the same rule. Callback arrival time does not detect gaps.

The leading silence between a declared selection boundary and its first input
buffer preserves alignment, without counting as a gap. Counted silence is
included in both WAV and manifest frames. A recording with only input gaps ends
as interrupted with `problemKind: gap`; a writer failure takes precedence as
`io`.

Native writes are synchronous on the tap's streaming thread, under the capture
lock, and a checkpoint can run there. Each tap therefore holds up to 10 s of
audio in a GStreamer queue ahead of that writer (`TAP_QUEUE`, the same capacity
as Mac's per-source writer queue), so a lock hold, a slow fsync or a stalled
tap thread delays the audio without losing it (R93-2). The queue keeps each
buffer's timestamp, so gap detection and clock anchors are unchanged. A stall
longer than 10 s still overruns pulsesrc's ring buffer. Under the gap rule
above, a loss of at least one second is saved as an `input` gap, which is
truthful but mislabels host-side loss until it is recorded as `writer` loss; a
shorter loss is diagnostic only, as host time the frames do not cover. A
checkpoint fsyncs only the WAVs written since the last one, so ended tracks
cost nothing. Stop drains each tap (bounded at 10 s) and records the queued
audio captured before the Stop moment, cutting the buffer that straddles it;
audio captured after it is dropped. The Stop moment is read when Stop is
pressed, before any capture or PCM lock a stalled checkpoint may hold, on every
Stop path. The end event is stamped at it, raised only to the last event and
every track end (D4), however long the drain takes. A tap that never received a
buffer has nothing to drain and is released at once; an armed others' tap
whose first buffer reached its queue drains like any other. In the native-only
recorder, a side still armed at Stop is released without a drain, as before
the queue (a follow-up).

`tests/test_native_timing.py` drives the real native input callback with the
pinned GNOME fixture and compares the entire manifest and WAV bytes. It also
simulates ten hours at +50 ppm without allocating the recording, and covers
threshold rounding, backward skew, long gaps, selection alignment and a partial
WAV-limit failure. Browser fallback tests independently exercise raw host stamps
and concurrent sessions. The private Pulse harness retains its existing sample,
duration, tone and bleed thresholds; it never connects to desktop audio.

A first native stamp more than 10 s ahead of the pipeline's running time is
re-anchored once per branch. When the lead is within 2 s of exactly 2^32
ring-buffer segments (from the pulsesrc's `actual-latency-time` and negotiated
rate, and only when that is a whole number of nanoseconds), that constant is
removed and GStreamer's own first stamp is kept. Otherwise the buffer is placed
one source latency (`actual-latency-time`, else the buffer duration) before the
running time at which it reached the appsink. Either way the wrapped segment
counter stops that source's skew resyncs for the rest of the recording, so its
stamps are sample counts rather than clock observations: no `clockAnchors` are
saved for it, and the app logs the shift, the path and the lost drift slaving.
The desktop CI job copies those lines into its job summary.

D57/R77: the re-anchored track also says so in session.json, as
`clockSlaving: {state: "lost", path: "exactWrap" | "runningTime", shiftNs}`
(the integer shift in ns). It is written under the capture lock before that
track's next PCM, so the following checkpoint and Stop keep it; a healthy track
never has it. Library publication copies it with the track's other absence
facts into x["codemyriad.io"].tracks, next to the span map whose missing
`clockAnchors` it explains. It is diagnostic only: no UI shows it, and readers
that do not know it ignore it. Browser captureRecord does not copy it, because
only native GNOME capture re-anchors and those sessions have no captureRecord.
