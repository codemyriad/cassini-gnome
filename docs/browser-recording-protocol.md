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
