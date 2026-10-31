# D36.1 checkpoint: capture, transport and mapped view foundation

Branch feat/recording-spectrogram-v1 starts from #137 at 5375f10. It carries the
parked FFT/producer core and its vectors from feat/recording-spectrogram (04fe579).
The old branch remains intact, including its historical native tap, for the port.
The current branch now merges #137's reviewed-fix head 73ba85c (20 hosted checks green).

Validation: 24 real libgstfft streaming/vector/source tests pass in the private
Ubuntu 26.04 container. Both native-size and 8192-frame blocks pass the frozen
published spectro-v1 vector and the extra DC/JFK candidate golden. The latter
still needs shared-byte adoption; UPSTREAM.json labels it as a candidate.

Native, browser PCM, per-session microphone and system-fallback taps are now
ported. They analyse committed selected PCM outside the recording locks, skip
readiness/off PCM, and retain the original sample grid after late subscriptions.
Tests prove saved PCM byte-for-byte, stereo streams, decoration failure, source
retirement and re-enabling. Application-kind packets now get the browser batch
bound before their first packet, preventing early sealing.

The private broker meter role and nonblocking GTK socket reader now deliver
bounded display columns, not PCM. Tests cover split JSON, independent sessions,
shared analysis, disconnect, completion and a failing view callback. The compact
display model covers adaptive delay, pooling, late drops, holes, silence and Off.

The GSK ring-texture component renders both sizes, wrapping, fading, theme
palettes and reduced-motion sweep. An isolated 1120x720 GTK test treats warnings
as fatal, checks fractional movement does not rebake a texture, and releases
textures on detach. Evidence: spectro-v1-current-base/spectrogram-components.png.
This is a component harness, not an application screenshot or frame-budget proof.

WindowSpectrograms now owns one frame callback per window and one NativeFeed or
BrokerFeed per meeting in that window. Several mapped pills/rows share its model.
Unmap, suspension and close release feeds, queues, textures and the frame callback;
remap starts cold. Completion unsubscribes before freezing the last mapped image.
Injected frame events check the 30 fps pill cap against 120 Hz row updates, and
unchanged frames do not rebuild event projections. Factory/render failures stop
only the decoration. CASSINI_SPECTRO=0 provides the measurement baseline.

The real GTK harness also maps the native FFT feed, proves PCM-to-texture delivery,
hidden zero-analysis, cold remap and completed unsubscribe. A real broker socket
integration proves that hiding the view unsubscribes without stopping recording.
A deterministic completion-during-frame regression covers the retirement race.

The modules are listed in Meson. Draft PR #139 is stacked on #137. There is no
application UI binding yet, so the normal application still performs no FFT.
Next: wire the Library pill and Recording page rows into the tested window hub,
including the application theme/reduced-motion signals and explicit page detach.
Run the required real-duration CPU/frame/memory cases on the completed path.
The old accelerated microbenchmark is not acceptance evidence. AEC stays parked.

Current checkpoint validation: 2082 source tests pass, two skipped (111.69 s),
with lint/format clean. The final feed/lifecycle group has 27 passing cases,
and the warning-fatal GTK component/native mapping harness passes. Source code
is unchanged after that full run. These are correctness checks, not the required
real-duration CPU, frame-pacing, RSS/owned-memory or four-source acceptance runs.
