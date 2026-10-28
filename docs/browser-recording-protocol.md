# Browser recording protocol

The canonical contract, schemas, shared extension and conformance vectors are
vendored under [browser-protocol](../browser-protocol/README.md). Its exact Git
revision is recorded in [PINNED_COMMIT](../browser-protocol/PINNED_COMMIT).

Run `python3 browser-protocol/verify.py --pinned browser-extension` to verify
both the Git snapshot and extension bytes. Update with
`build-aux/vendor-browser-protocol.sh /path/to/upstream FULL_COMMIT` after the
shared change has landed upstream. Install `browser-protocol/requirements-test.txt`
to run the request-derived stdio conformance suite.


The GNOME app uses the optional `independent-sessions-v1` control capability.
Library Record snapshots every currently active browser call. Each owns its
capture ID, folder, native microphone tap and browser remote tracks; a page's
Stop ends only that session. The Library pill, portal shortcut, hidden native
notification and Quit stop all sessions. New calls join only with automatic
recording enabled. A generation-bound application lease keeps automatic capture
independent of window visibility and prevents a late handshake from undoing Quit.
Legacy peers retain the original second-start refusal and v1 vectors.

What is recorded changes only subsequent audio. Me, The others and Both release
disabled native sources, finalise their WAVs and create fresh WAVs with offsets
when enabled again. Browser packets are still checked for sequence and PCM
geometry while their side is off; their samples are discarded. Optional
`sourceTrackId` preserves a browser speaker across spans without using labels
as identity. Native microphone/system spans retain Me/The others.

The wave-6 S-6 contract is pinned in
[SESSION-EVENTS.md](../tests/ux/SESSION-EVENTS.md). It uses version 1's optional
fields: each `selection`/`othersSource` event shares one sync-and-rename checkpoint
with the track change it explains, before acknowledgment or PCM use. Shared
fallback/off-on fixtures and hand-written expectations live in
[session-events](../tests/ux/session-events/README.md).

A confirmed rejected browser Start uses a per-meeting system fallback; an
unknown/timed-out command is not blindly retried as a second capture. Loss of the
browser audio connection starts system capture in the same folder without
restarting its microphone. It does not automatically switch back. The Recording
page and hidden notification explain the fallback. A local browser-process hint
(PID plus kernel start tick) retains existing tab/capture bindings across worker
restarts. It is neither participant identity nor a change to the shared protocol.

Native system sound monitors the sink **after** its volume/mute controls. It can
be silent when speakers are muted, unlike the companion's source capture.
Capturing per-application audio before sink volume is a later improvement, not
part of wave 6. A system-source failure never silently switches to microphone-only.

The approved extension ID is `pcafdaahfhjiolfpfnlhmljfgoabbaij`. Companion setup
opens its Web Store URL in a detected browser and writes both per-user native
host manifests with this exact origin. Add and Connect do not fabricate a
connection: Done requires the real hello/status handshake. The developer test
browser can still load the pinned extension directly; that is not the end-user
installation flow.

Until W6-F supplies the Flatpak host bridge, that build hides the first-run and
Meeting setup links, the Settings setup button and browser-only automatic-record
switch. Its Browser companion status remains Not set up. Record still captures
the microphone and this computer's sound. Both manifests grant only
`xdg-music/Cassini:create` for the localized default Library; the installed
package smoke verifies PCM/manifest persistence across two sandbox launches.
Settings hides Move until a document-portal location is proved durable across
restart. No broad home permission is granted. W6-F follows What is recorded and will
provide host browser launch/registration and an app relay; no additional Flatpak
permission is added by UX10.

Run the pinned Node unit/conformance suite with
`node --test browser-protocol/extension/tests/*.test.cjs`. Its optional independent
vectors live beside that canonical tree. The installed `browser-extension` copy
is proved byte-identical first by `verify.py`; real browser tests load that copy.
