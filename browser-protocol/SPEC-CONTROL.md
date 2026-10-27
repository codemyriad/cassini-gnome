# App/browser control — v1

This separate channel lets Cassini show and record meetings in the browser. It does not
change the audio host, audio SPEC, or the 88 audio vectors. The extension maintains a
persistent Native Messaging connection to **com.codemyriad.cassini.control**; PCM still
uses **com.codemyriad.cassini**, one connection per recording. Either host may be missing
without breaking the other. Losing control never stops or deletes a recording.

## Transport and trust

Use native-endian uint32 byte length + UTF-8 JSON, 1–524288 bytes, exactly framed, stdout
reserved for frames. Objects reject duplicate members and non-JSON numeric tokens. Version
is the integer literal 1. Each control host has its OWN manifest and explicit allowed_origins
(no wildcard); installing the audio host does not authorize a control origin. The native
host/broker keeps browser connections distinct: tabId is scoped to one browser/profile,
not globally unique. The broker must namespace peers and route commands to the right peer.

A local broker uses a user-only Unix socket (0600 in a 0700 directory; GNOME's default is
$XDG_RUNTIME_DIR/cassini/browser.sock), or equivalently protected IPC. Validate ownership
and peer identity; do not expose a TCP listener. Production broker IPC is platform-owned.
Conformance uses an isolated socket with NDJSON envelopes and CASSINI_CONTROL_SOCKET;
a host launcher may adapt its private IPC to this test interface. It must not use or touch
the user's running app/socket, desktop, tabs, devices or recordings.

**Page content may report counts only, never titles/URLs, start/stop, or paths.** Background
gets tab metadata from Chrome. A page's reports are advisory discovery hints, not verified
participant identities or permission to record. Audio commands retain the existing token
and request checks. An untrusted page cannot send management commands through its relay.

## Messages

Extension → host, immediately on each connection, in this order:

- `{"type":"hello","version":1}`
- `{"type":"meetings","meetings":[{"tabId":2,"title":"Team call","url":"https://meet.google.com/example","sourceCount":3}]}`
- `{"type":"status","recording":false,"tabId":null,"path":null,"message":"Ready. Choose Separate tracks or Whole tab.","tracks":0,"stopping":false}`

Meetings/status are complete replacement snapshots, also sent when state changes. The initial
list may be empty while page discovery answers; later snapshots fill it. Entries aggregate
live frames by tab. Zero discovery removes an idle candidate; a recording row remains even
with zero sources. Navigation/closed tabs/removed frames clear stale counts. Titles/URLs
come only from tabs.get. No audio is captured for discovery. Reconnect sends hello and fresh
snapshots, not a fabricated stop or a replayed start. App policy must reconcile that status
before auto-starting. Snapshot delivery may coalesce; PCM packets never enter this channel.

Host → extension:

```json
{"type":"command","requestId":"record-1","action":"start","tabId":2,"mode":"tracks"}
{"type":"command","requestId":"stop-1","action":"stop","tabId":2}
```

Start resolves the explicit tabId with tabs.get; it MUST NOT use the active tab. It requires
a currently discovered source. Only `mode: "tracks"` is accepted from the app. **Whole tab
requires a click in the browser** (activeTab); the app cannot obtain that invocation grant.
Stop is scoped to tabId: it cannot stop another tab's recording. An idle stop is idempotent.
The popup and app share one recording. A second start in either place is refused with the
same exact message: **A recording is already running. Stop it before starting another.**
The app/broker coordinates its own native capture ownership as well.

Extension → host:

```json
{"type":"result","requestId":"record-1","ok":true,"status":{"type":"status","recording":true,"tabId":2,"path":"/recordings/Recording-example","message":"Recording separate tracks · 1 recorded track","tracks":1,"stopping":false}}
{"type":"result","requestId":"bad-1","ok":false,"error":"Choose a meeting to record."}
```

Commands execute in arrival order. A valid requestId always receives a result while connected;
an invalid command gets ok:false with an error, and no audio side effect. Missing/invalid
requestIds cannot be correlated and are ignored. Duplicate IDs on the same connection reuse
the original result, even while pending. Remember up to 128 requests, evicting only completed
oldest results; never re-execute a pending duplicate. Deduplication is connection-local; after
reconnect the broker must reconcile the fresh status, not blindly resend a start. A disconnected
peer's queued commands are discarded; an already executing command may complete, and its
recording continues. No management message supplies a filesystem destination.

| Field | Type / limit |
| --- | --- |
| requestId | nonempty string, ≤128 Unicode code points, or nonnegative safe integer |
| tabId | nonnegative safe integer; null only in status when unknown |
| meetings | ≤100 entries, active row first; sourceCount 0–4096 |
| title / url | strings ≤200 / 2048 code points; web URLs only |
| status | recording bool, tabId nullable, path nullable string, message string; optional tracks integer ≥0 and stopping bool |
| action / mode | start or stop; start requires tracks |
| error | string ≤2000 code points |

Unknown fields are ignored. Unknown host message types are ignored. A missing host uses
bounded 1–30 second retry plus a one-minute alarm that survives MV3 worker idling; do not use
Chrome-150-only alarm persistence options. Permissions added: tabs (background metadata),
alarms (reconnect). The minimum remains Chrome 116.

## Page discovery interface (GNOME owns page-hook.js)

Background/relay sends `{channel:"cassini-control",type:"discover"}` independent of any
recording token. Page hook answers and publishes count changes with
`window.postMessage({channel:"cassini-page",type:"discovery",sourceCount:N}, "*")`.
N is an integer 0–64 per frame. It must scan without an AudioContext, capture or native audio
session. Relay validates count, forwards only `{type:"discovery",sourceCount:N}`, caches it
for reconnection, and never forwards page start/stop/management payloads. Publish zero when
sources disappear. Until the page-hook implementation ships, the idle list stays empty.

## Product behavior shared by both apps

Show **Meetings in your browser** under the Record control, with Chrome title and service if
recognized. Each row has **Record**; while recording it becomes the live indicator. Never
expose tab IDs, frames, sources or tracks in app UI.

Auto-record is OFF by default. Settings label: **Record every browser meeting automatically**.
Explain once that future browser meetings will be recorded locally, and show a notification
with **Stop** for every auto-start. This policy belongs in the app, not in the extension.
The companion connects automatically: **Listening inside your browser**. The app starts
only separate-tracks mode. Whole tab requires its browser click.

Two UX points are awaiting Silvio's decision after GNOME supplied counterexamples: converting
sourceCount into a people count (mixed receiver slots and sender/receiver pairs are not
people), and automatically broadening a selected browser meeting to computer/microphone
capture on failure. These are not control-wire semantics. This transport never estimates
participant identities or initiates desktop capture. The interim app wording can be
**Audio available** / **no audio yet**, with an explicit **Record this computer** action;
if native capture is selected, its indicator is **Recording this computer’s sound**.
Native permissions and consent apply. Both apps must adopt the same resolved product choice.

Mac follow-up after this PR: the same app-side list/Record/live indicator, protected local
broker/control host with its own allowlist, opt-in Settings switch, notification with Stop,
and native fallback. GNOME owns its broker/UI and page discovery in this rollout.

## Verification without a desktop

`node --test extension/tests/*.test.cjs` runs actual background/relay/control code in VM
fixtures; control cases are in conformance/control/vectors.json. The host runner is
`python3 conformance/control/run.py HOST_OR_TEST_LAUNCHER`: it creates isolated protected
IPC and verifies both directions against those same envelopes. Neither runner launches
Chrome, the app UI, microphones or other audio devices. The existing audio runner remains
unchanged and runs alongside them with a headless host fixture in shared CI; app CI also
checks the real native audio host. Schemas live in schemas/control-{extension,host}.schema.json.

Chrome API basis: [Native Messaging](https://developer.chrome.com/docs/extensions/develop/concepts/native-messaging),
[tabs metadata permissions](https://developer.chrome.com/docs/extensions/reference/api/tabs),
[alarms and pre-150 persistence](https://developer.chrome.com/docs/extensions/reference/api/alarms).
