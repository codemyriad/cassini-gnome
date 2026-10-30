# Browser recording protocol

The canonical contract, schemas, shared extension and conformance vectors are
vendored under [browser-protocol](../browser-protocol/README.md). Its exact Git
revision is recorded in [PINNED_COMMIT](../browser-protocol/PINNED_COMMIT).
The current pin, `6440a50`, ships companion 0.2.2 and accepts the reserved `othersSource.reason: restored`
for readers. GNOME reads its explicit source state without rewriting the file;
writers still reject this reason and never switch back automatically.

Run `python3 browser-protocol/verify.py --pinned browser-extension` to verify
both the Git snapshot and extension bytes. Update with
`build-aux/vendor-browser-protocol.sh /path/to/upstream FULL_COMMIT` after the
shared change has landed upstream. Install `browser-protocol/requirements-test.txt`
to run the request-derived stdio conformance suite.

## Native host migration for 0.3.0

Protocol `7f8c3e8` ships extension 0.2.1, using only
`io.codemyriad.cassini` and `io.codemyriad.cassini.control`. The extension key,
ID and store URL are unchanged. Reconnect through Connect in Cassini after
updating; do not rerun the developer installer with an old extension ID.

On startup, cleanup runs on a daemon worker, independently of Connect and without
blocking GTK or Quit. Connect and the developer installers also clean old
registrations. Cleanup requires an exact legacy file name, a matching JSON name
and one of Cassini's own executable basenames. Allowed origins never determine
ownership. Malformed, oversized, ambiguous, non-regular and symlink files stay
untouched. Native cleanup checks file identity/mtime before removal; Flatpak
reads and compares/deletes on the host through flatpak-spawn, without host Python
or added permissions. A modified host file is preserved. Only the new io names
are registered; there is no dual-registration switch.


The GNOME app uses the optional `independent-sessions-v1` control capability.
Library Record snapshots every currently active browser call. Each owns its
capture ID, folder, native microphone tap and browser remote tracks; a page's
Stop ends only that session. The Library pill, portal shortcut, hidden native
notification and Quit stop all sessions. New calls join only with automatic
recording enabled. A generation-bound application lease keeps automatic capture
independent of window visibility and prevents a late handshake from undoing Quit.
Legacy peers retain the original second-start refusal and v1 vectors.

For companions advertising `call-evidence-v1`, automatic starts require both a
positive receiver count and `heard: true`. The companion counts live audio
receivers on connected peer connections, never microphone senders. The latch belongs
to peers that supplied evidence. First reports establish counter baselines; each
vote requires at least ten new inbound audio RTP packets, with two voting ticks
in three. A five-second handoff grace cannot be cancelled by an owner returning
after its stored deadline. Connected pre-join receive slots alone do not qualify. Missing
or malformed heard evidence fails closed. GNOME rechecks this rule for queued
starts and before reserving a new automatic system fallback after a rejected
browser Start. Manual Record, Record-all and legacy companion policy retain
their existing behavior. This does not change when an existing recording ends.

The unchanged shared [app vectors](../browser-protocol/conformance/control/auto-start-vectors.json)
run through production inventory handling, automatic scheduling/dispatch and manual
Record candidate selection in `tests/test_browser_call_evidence.py`. All ten cases
run for each path in the existing source, Meson and Recording pytest gates. The
transport acknowledgement is stubbed; real companion/audio integration remains in
the isolated Recording suite. No application guard or vector expectation is rewritten.

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

The Flatpak build uses `flatpak-spawn --host` to find and launch host browsers
and to register both native hosts in the host's per-user configuration. Small
host-side shell relays run the matching installed Flatpak application/branch;
native-messaging frames stay on stdin/stdout, without a host Python dependency.
The relays carry a local browser process/start-tick hint across the sandbox so
worker reconnects retain their capture bindings. Native, regular Flatpak and
development Flatpak brokers have separate runtime addresses.
The broker runs in its own sandbox instance: a child broker would keep the UI
or native-host `flatpak run` wrapper alive. Host browser launch is detached for
the same reason. The dedicated broker exits after ten idle seconds with no
browser, application lease or unfinished capture.

This bridge uses only `--talk-name=org.freedesktop.Flatpak`, as approved in D23.
When its effective permission or host service is unavailable, first-run/Meeting
setup links, Settings setup and browser automatic-record stay hidden. Settings
shows Not set up; Record still captures microphone/system sound. Both manifests
retain exactly `xdg-music/Cassini:create` for the localized default Library;
the installed smoke proves byte-identical PCM/manifest persistence across two
launches. Settings still hides Move until document-portal relocation is proved
durable across restart. No broad home permission is granted.

`tests/flatpak/companion_bridge.py APP_ID --output evidence.json` tests the
installed permission, actual host Add/registration and hello/status handshake,
then two independent native pipes with 96,000 byte-exact PCM frames each and
individual Stop. It uses a private HOME/XDG/session bus and no added grants.
Host stderr and sandbox diagnostics sit beside its JSON evidence. The package
smoke runs this after the Library restart proof. Unit tests additionally execute
the real relay with shell metacharacters in host paths and verify browser process
identity survives two relay launches.

For an actual loaded-browser proof against a private installed bundle, use
`env -u CASSINI_LIBRARY_DIR CASSINI_TEST_FLATPAK_ID=io.codemyriad.CassiniTalk.Devel tests/recording/run.sh node
tests/recording/flatpak-companion.mjs` (set `FLATPAK_USER_DIR` for a private test
installation). It loads the installed pinned extension, records two calls with
their microphones off and both tabs/sink muted, checks original tone/bleed
bounds, kills/wakes the worker, and verifies system fallback, the same folders
and individual/global Stop. PCM/manifests and diagnostic JSON are kept in its
isolated recording artifact. It never opens the real browser profile or desktop.

Run the pinned Node unit/conformance suite with
`node --test browser-protocol/extension/tests/*.test.cjs`. Its optional independent
vectors live beside that canonical tree. The installed `browser-extension` copy
is proved byte-identical first by `verify.py`; real browser tests load that copy.

## Browser call lifetime

For independent captures, a reachable page reporting zero call sources ends the
meeting after a two-second grace; it does not select system sound. Losing the
page or companion invokes Q4. A fallback keeps its existing folder and sources
when an active call returns, without switching back to browser audio. If no
active call returns within 30 seconds, Cassini stops and saves it, including when
the browser connection itself is gone. The broker advances these deadlines on
busy accept traffic as well as idle timeouts.

The private recording harness proves hang-up with a tab left open, reload,
worker reconnection, expiry without an active call, and unaffected simultaneous
meetings. Unit tests advance only the recorded missing timestamp to exercise the
same deadline branches without wall-clock sleeps.
