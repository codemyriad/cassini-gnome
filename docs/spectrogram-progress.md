# D36.1 implementation and acceptance

The Library has a two-lane 50x20 pill; Recording has 200x32 rows. Mapped
views of a meeting share one model/subscription and a window frame callback.
Hidden, suspended and detached views release their feeds and textures. Theme,
reduced-motion sweep, selection, warnings and stopping freeze/fade are wired.
PCM enters the observer only after admission, outside capture locks. Saved WAVs,
AC-level silence decisions and recognition inputs are unchanged.

Analysis uses GStreamer's actual libgstfft through ctypes. A small Meson-built
C helper handles mono conversion, frame mean/window, band sums and display codes.
There is no NumPy indicator path. Both native and scalar paths run the frozen
raw/display vectors, including both input chunk sizes and stereo precision.
The helper lives under the private libdir, has an ABI check and an installed
smoke check, and participates in build identity and partial-update verification.
Packagers retain its identity-hashed bytes. CI build products carry the same
helper to the GTK/recording consumers.

The extra DC/JFK golden is still labelled as a shared-publication candidate in
UPSTREAM.json; it has not been represented as independently approved bytes.

The warning-fatal component and real native/browser application tests run in
private Xvfb. The browser probe is a separate real broker process with native
microphone and four remote PCM streams; at most three remote tracks are analysed.
The private PulseAudio journey keeps its existing duration, tone and bleed checks.

The 60-second A-E measurement scripts use the real GLib main loop and start frame
sampling after warm-up. Reports include a source/resource/helper digest and reject
source changes during measurement. They retain elapsed and thread-CPU counters,
whole-process CPU/RSS, late columns and owned rendering state. CPU work is checked
against CPU budgets; frame intervals independently cover stalls. Cairo/Xvfb data
is not a hardware GPU drawing-cost claim. Native frame data from older reports
included warm-up and is diagnostic only. Internal state accounting excludes shared
interpreter/library/driver allocations, which are visible in process measurements.

Run `tests/spectro/check_measurements.py` on one A/B/C/D/E report each. It applies
the published limits without exceptions. Until all required measurements, the
remaining visual/minimization matrix and hosted checks pass, PR #139 stays draft.
