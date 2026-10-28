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
