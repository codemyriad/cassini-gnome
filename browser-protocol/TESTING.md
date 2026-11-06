# Layered recording checks

1. Portable raw-JSON stdio vectors in conformance/, against both executable hosts. Each case
   gets a private `CASSINI_LIBRARY_DIR`; a host that writes elsewhere stops the run
   (tests/test_library_isolation.py guards the real user Library). Includes
   null/type/lexical-number rules, nonfinite input, sequence, silence gaps, EOF, errors and WAV
   finalization. Swift additionally tests malformed/truncated/oversize native framing directly.
2. Portable extension worklet/clock/buffering unit tests (extension/tests/*.test.cjs).
3. Browser scenarios use pinned Playwright 1.61.1, Chromium 149.0.7827.55. The shared tone
   primitives are in scenarios/; Mac adapters are BrowserExtension/tests/*.mjs. Distinct
   440/880 Hz peers and 660 Hz replacement verify spectral separation (<1% power bleed),
   persisted-sample mute, shared timeline (<400 ms), sender detach/restore, 3-second host
   SIGSTOP, 35-second idle/explicit worker termination, iframe capture, navigation and real
   stereo tabCapture/offscreen cleanup. Fixed waits create the intentional idle/stall/mute
   intervals; readiness/finalization use observed states and recorded frame counts.
4. Native Swift sink uses injectable writer and synthetic AVAudioPCMBuffers: one hour ±50 ppm,
   per-source bounded stalls/tail silence, quantization/NaN, planar/interleaved 1/2/4 channels,
   format change, failure once and late callbacks. Loader tests include GNOME timestamps,
   corrupt/empty metadata, stale frame counts, and escaping symlinks.
5. Hardware-native validation is an explicit manual gate. On Mac run
   scripts/check-native-recording.sh after real system-audio permission is granted. The
   sink-only variant does not establish CoreAudio/TCC/device compatibility. Microphone testing
   needs an actual input device. CI builds the app but cannot grant privacy consent.

Hosted Mac CI runs the contract, native writer/loader and WebRTC/media scenarios on macos-15 /
Xcode 16.4 and builds the signed app with its bundled host. Real tabCapture runs on DrMeyers:
hosted macOS has an absent/intermittent Null Audio Device and is not a reliable rendered-tab
audio gate (https://github.com/actions/runner-images/issues/13668). The native workflow is
manual/nightly only. Its sink-only mode also runs tabCapture; full mode additionally attempts
the consent-dependent CoreAudio tap. Local validation uses macOS 27 / Xcode 27.
Failures preserve generated audio/metadata and logs as CI artifacts. No real conversations
or microphones are used in unattended tests.

GNOME has separate real user-gesture/Xvfb, PulseAudio/PipeWire, GTK and Talk harnesses. Their
results are not Mac hardware evidence. A fully merged browser
scenario driver, MLS time-code/Talk HPB fixtures, cross-browser minimum-version runs, and
nightly soaks remain explicit migration work, not implied by a green vector suite. The canonical upstream is codemyriad/cassini-browser-protocol; the Mac app owns its approved
manual/nightly DrMeyers runner. Neither runner setup nor a synthetic fixture grants TCC consent.

SPEC v1.1 adds checked-in macOS/GNOME native-session WAV fixtures, offline Git pin-verification
regressions, and controlled Teams mixed-receiver / Zoom PC-free / Meet switching-slot browser
models. Product account/version observations remain manual in products/results/.


The independent-session capability adds `tests/independent-sessions.mjs`: two
real Chromium WebRTC calls, separate native host connections and folders, both
tabs muted and an explicitly private Pulse sink muted. It checks two isolated
tones, media-clone deduplication, and one meeting continuing after the other's
Stop. It requires CASSINI_ISOLATED_AUDIO=1 and validates the private Pulse socket
and sink before mutating them; it refuses to run against a desktop sound server.
Use the GNOME private recording harness locally. Hosted CI starts its own server.
The default audio writer/control bridge are test fixtures, not product hosts.
CASSINI_NATIVE_HOST and CASSINI_CONTROL_NATIVE_HOST select actual app adapters;
CASSINI_TEST_EXTENSION and PLAYWRIGHT_MODULE select a pinned installed extension
and local Playwright. The generated launchers force all output into the private
fixture Library. Failure artifacts keep recent control events and manifests.

S-6 adds `conformance/session-events/`, copied unchanged from the coordination
fixtures with their origin recorded. `python3 conformance/check-session-events.py`
checks the schema, finite/ordered event times, the source-ID UTF-8 byte bound,
hand-written reader expectations and generated sparse WAV timelines. Unit tests
cover legacy/null annotations, unknown event types/fields, selection bounds and
the 1000-event cap. Neither schema validation nor these silent files prove a
writer's atomic event/track checkpoint: both apps must test crash/failure at
their real writer boundary and read these same fixtures independently.


D33 adds14 literal monotonic lifecycle vectors, checked by
`python3 conformance/check-call-lifecycle.py` and adopted unchanged by both apps.
The actual loaded-extension independent-session test also reloads a live call,
checks the original native connection/capture ID/folder and renewed PCM, closes
another tab, and verifies explicit terminal reasons through the fixture writer.
The fake-control driver does not prove desktop fallback or2s/120s real app timers;
those require the desktop's own adoption tests. Unit cases keep legacy loss behavior,
late old-document events, subframe bounds and invalid Stop side effects covered.

## D52 multi-stream wave 1

`python3 -m unittest discover -s tests -p "test_*.py"` checks the hand-written
lifecycle, outcome and half-open ownership vectors, actual generated public-speech
WAVs, per-source refusals, UTF-8 identity bounds and schema/pinning integrity.
The corpus v2 tests (`test_multistream_corpus.py`, `test_corpus_oracles.py`) run
`shape()`, with one assert per requirement and the thresholds read from
`corpus.json` `constants`. At least one data mutation per requirement must fail
with that requirement's message. The hand-written `expected` literals are checked
against `ownership.py`, `gaps.py` and `floor.py`. Patching `gaps.presence`,
`floor.span` or `floor.person` must make that same check fail; the floor rules the
corpus cannot show (gap masking over non-zero samples, mean removal) have mutants
that must fail synthetic checks. The stored WAVs are checked for every expected
bleed (target minus its own carrier), the three-voice residue, byte-identical D49
and seam overlaps for the pairs `shape()` finds, the noise bed, the floors, and the
pinned digests. Only the schema check needs jsonschema; the rest of the guard runs
on a bare `/usr/bin/python3`.

`test_fallback_corpus.py` holds the D63 vectors. It checks the per-track and pooled
active-seconds bands, the fallback windows and the per-person fallback against
`activity.py` on the stored WAVs, `fallback.json` against `activity.decide()`, and the
turn and word keep/drop cases in `corpus.json` and `writedown.json` against
`writedown.py`. A guard requires a person who falls back only when pooled; each guard
message has a failing data mutation, and code mutants of both oracles must fail the
same literals or, for rules the corpus cannot show, synthetic checks.

`npm test` includes injected-clock retry/long-suspension cases and real Chromium
pre-join, source-switch, call-generation, worker-recovery and independent-session
cases. A receiver switch waits for the actual replacement waveform with unchanged
sample, power and bleed thresholds. Call-generation tests reuse a receiver,
exclude late old peers, and preserve intentional `callChanged` retirement.
Run only with a private audio server. Network-isolated containers need a private
bridge with a route for ICE, not a desktop audio/bus/home mount.

## D58 classified refusals

`conformance/refusals/` holds the injected-refusal writer cases, the wire cases for
`ok:false` result codes and the Details reader lines, with an independent oracle.
`tests/test_refusals.py` checks the oracle against every case, mutates it to prove each
R79 guard fails a case, and validates every expected list against the session schema.
CI cannot produce a real TCC or device denial: each app injects the refusals through its
own start path, writer and reader. `extension/tests/result-codes.test.cjs` checks the
code the extension sets at each `ok:false` site.

## Shared test 9: placement golden (D55)

`conformance/placement/` holds three tone-burst sessions (`make.py`, pinned by
`materialized.sha256`), the hand-computed D55 rounding literals (`placement.json`)
and every burst's expected position per consumer (`bursts.json`), which must equal
the independent oracle `placement.py`. `tests/test_placement.py` checks the
literals, proves that every wrong rounding seen so far (half-even, floor,
ns-then-ceil, whole-ms word base, whole microseconds, ms / 1000 * rate in
doubles, dropped or doubled offset) contradicts one and misplaces a recognized
burst in the folders, reads the WAVs back through the shared onset detector,
validates the sessions, and proves the results checker rejects a one-sample
shift on the exact tier, a 25 ms shift on the 20 ms headline, a missing burst
or leg, NaN or fractional positions, a missing or wrong `offsetsAssumedZero`,
and a played session with an unknown offset. Each app runs its own consumers on
the folders and checks its results file with `placement.py check`, then repeats
on the three app mutants (`make.py --mutant`, `check --mutant`) so that numbers
copied from `bursts.json` fail; see the folder's README.

## Shared test 2: live folder oracle (D68)

`conformance/live-folder/` holds the scenario constants and the `evidence.json`
contract (`scenario.json`), the coordinator-owned oracle (`check.py`) and a
synthetic passing run with 40 named mutants and 4 variant writer forms (`make.py`,
pinned byte for byte by `materialized.sha256` on Linux and macOS Python 3.9). The
shared driver runs the scenario against each app's real writer: a 60 s
`replaceTrack(null)` mute with A the only open source, a late remote, a connected
slot that never sends, Me off then on at 90 s, and a forced resume (SIGKILL of the
audio host: system/lost, one `resume-capture-v1`, browser/restored with new spans
under the same keys). The driver reads each resume request off the app's control
pipe itself, copying the host's stdout through a relay while the launcher execs the
host, which stays the browser's direct child, and records each request with the recording
identity it names and its time (`scenario.json` `resumeObservation`).
`check.py check` then checks the folder against the evidence brackets and those
requests, the scenario and corpus constants and PCM it measures itself, never
through an app reader; `check.py compare` lines two apps' results up.
`tests/test_live_folder_oracle.py` proves the base run and the variants pass, each
mutant and evidence fault fails exactly its rules, every rule is shown failing at
least once, GNOME's rotated `spans` are read piece by piece and must tile the logical track, and that `check.py`
needs only the standard library. See the folder's README for every rule.

### The driver (`scenarios/live-folder.mjs`)

`runLiveFolder({meeting, remote, app, worker, out})` runs the scenario through an app
adapter (`record`, `selectMe`, `snapshot`, `meetingRow`, `stop`, `audioHostPids`,
`folders`, `controlHost`, `browserPid`, `cleanup`, optional `saved`; the header of the file lists the contract), writes
`evidence.json` and runs `check.py` before the app quits. It needs no Playwright of its
own: the adapter passes the pages and the extension's service worker.

- Teardown, on every path (a `finally`): if Record was asked and Stop was not, the driver
  asks `stop()` (the stop bound); then it awaits the adapter's `cleanup({failed})` once,
  bounded by `CLEANUP_MS`; then `stopRelays` ends any control relay still running and
  `stopLaunchers` kills any control launcher still waiting in `open()` on its FIFO (its
  relay never opened it: the launcher reads neither of the browser's pipes, so the
  browser's exit does not end it). Every identity is read from the process's exact argv
  (`/proc` on Linux; on macOS the kernel's `KERN_PROCARGS2` through `python3`, since `ps`
  joins arguments with spaces): a host runs its whole command as consecutive arguments, a
  relay has `control-relay.mjs` and a launcher its launcher path as one whole argument, and a
  zombie is none of them. So a recycled pid, a similarly named process or a launcher still
  waiting is never taken for the host or the relay. A relay spawned but not yet
  started at teardown cannot be found by its log line; it watches its writer instead (the
  launcher's pid, passed as its third argument) until its FIFO is open, and once that pid
  is gone it opens the FIFO for writing itself without blocking and closes it, so it reads
  the end of its input (`writerGone`, then `end`) and exits. `check.py` is bounded by
  `CHECK_MS`: one that does not finish is killed and fails the run, which is then torn down. `cleanup()` ends everything the adapter started (the
  app and its recording, the browsers, its broker and hosts) and leaves no process; it is
  idempotent, so the adapter may also call it from its own `finally`, and it leaves
  `controlHost.observe` in place for the driver. It runs after a failure's
  `evidence.partial.json` and after `check.py`. `out/teardown.json` says what each step
  did; a step that fails never skips the next, never hides the run's own failure, and
  fails a run that otherwise passed.

- Topology: `connectReceiveSlots(meeting, remote, [[A], null, null], {play: true})`, three
  recvonly slots on one peer. A `null` slot is connected and never gets a track; `muteSlot`
  mutes a slot with `replaceTrack(null)`, so no RTP flows (the real SFU shape). `play`
  makes the page play each slot through an `<audio>` element, as meeting pages do:
  nothing pulls an unplayed receiver, so its jitter buffer fills until the capture starts
  and the recording then lags the sender by 1-2 s (measured on roy: about 50 ms when
  played). That lag carried A's tone up to 1.4 s past the mute and failed O2.silent in
  two of six runs; the driver records A's jitter-buffer delay in `evidence.json` notes.
- Clock: `scenarios/folder-clock.mjs` reads `session.json` (tolerating a missing or
  half-written file) for S, the latest track end, and F(source), one source's. Every
  step waits on the folder or the app with a bound from `scenario.json` and fails with
  the step's name (E1). Times are on the session timeline of `scenarios/session-clock.mjs`:
  the origin the extension holds (a wall-clock `Date.now()`) is mapped onto the system's
  monotonic clock once, as SPEC requires of capturing documents, and the wall clock is never
  read again, so a drifting or stepping wall clock (a hosted VM's ran 500 ppm fast) moves no
  requested or observed time. The mapping is at the origin's own instant: the driver reads
  (monotonic, wall) pairs from before Record until the extension reports the origin (every
  `ORIGIN_SAMPLE_MS`, 10 ms, while Record runs) and places it between the two that bracket it,
  as an app that maps it when recording starts does. A wall-clock step is never interpolated
  across: the origin is placed on the side of the step it was read on, and a step that leaves
  that side ambiguous fails the run explicitly. One
  read when the driver learns the origin, 2.35 s later on a wall clock slewing 510 ppm, put a
  selection the app stamped at once 1.2 ms before its request (Mac #149, O5.selection). They are app-neutral: the `chrome.storage.session` `cassiniSessions[]` entry of
  the meeting tab gives the evidence's `captureId` and integer `timelineOriginMs`.
- Mute: asked once F(A) and the session clock both reach `MUTE_START_MS`, unmuted once both
  reach `mute.requested + MUTE_MS` (O2.muteStart, O2.duration).
- Forced resume: `scenarios/native-launcher.mjs` registers the app's production hosts
  through a POSIX `sh` launcher that writes its pid to `<pids>/<name>-<pid>.pid` and execs
  the host, so the test kills exactly the session's audio host. The driver waits for
  `system/lost` before `browser/restored` (a restore with no committed loss fails at once).
- Resume requests (`scenario.json` `resumeObservation`): the adapter registers its control
  host with `writeNativeHost({..., observe})`. That launcher takes the first free
  `<observe>/<n>` (mkdir, so connections count from 1 in launch order), records `$$ $PPID`
  there, makes a FIFO in it, starts `scenarios/control-relay.mjs` detached (stdin
  `/dev/null`, stdout the browser's pipe) and execs the host with only its stdout into the
  FIFO. The launcher keeps its files private with `umask 077` and gives the host the
  browser's umask back before the exec. The relay copies the FIFO to the browser byte for
  byte, one blocking write per read, logs each native-messaging frame (stamped once the
  frame is whole, before forwarding: `atWall` for people, `atMono` on the same monotonic
  clock for the evidence) and exits at the end of its input; an unreadable
  frame is logged and still forwarded. When the browser closes its end, the relay learns it
  from the next forward (EPIPE), logs `outputError` with the frames it did not deliver
  (`undelivered`: still resume requests, the host asked, written with `delivered: false`)
  and closes the FIFO: the host's first write after the
  close still succeeds into the FIFO, its next one fails as without the relay (Node cannot
  poll a pipe's write end for the hangup). At setup, record, kill, restore and stop the
  driver fails the run if a control host's launch parent or live parent is not
  `browserPid()`, a relay logged an unparsable frame or an input error, a relay has not
  started within `RELAY_START_MS` of its launch (the host then waits on the FIFO), or a
  relay stopped before the end of its input (a relay found gone is judged on its log read
  again, by every one of these checks: it may have logged its end, or a fault, just
  before exiting); `notes.browserPid` and
  `notes.controlParents` keep what each check saw. After the stop observation it writes
  `resumeRequests` in pipe order.
- The mute is held until A's own frames cover `MUTE_MS`; on every poll A must keep one
  span, stay open and see no `othersSource` event. Me on waits for the microphone span
  that starts at the `me:true` selection's `atMs`.

`node --test tests/live-folder-driver.test.mjs` checks the folder clock against
`make.py`'s synthetic folder, its tolerant reads and labelled bounds, the launcher
(pid, arguments, exec, host-name prefixes), the constants the driver reads, and
`runLiveFolder`'s teardown with fake pages and app around a real launcher and relay (a
failure after Record: Stop, one cleanup, the relay stopped; a failure while the control
launcher starts: one cleanup, the launcher waiting on its FIFO killed; a failing or missing
cleanup never hides the run's failure; a relay that starts only after teardown killed its
launcher ends by itself; the waiting launcher is never live and is still killed; a recycled
pid running the same interpreter, or the host's path only as an argument, is never the host
(paths with spaces); a process named like the relay or the launcher (`control-relay.mjs.backup`)
is never signalled; a `check.py` that never exits is killed at its bound; CI runs this file
on macOS too, in `launcher-macos`);
`node --test tests/control-relay.test.mjs` checks the frame reader across chunk
boundaries, the relay's byte-exact passthrough, unparsable and cut-off frames, and the
observing launcher (the host stays the spawner's child, connections in launch order, the
parent check, resume requests in pipe order, undelivered requests kept and marked, a relay
that logs a fault and exits between the first read and the liveness check, stuck relays stopped, the browser-to-host
bytes untouched, the host's umask, a relay that never starts or dies, the browser closing
its end on a real pipe) with no browser.
Protocol CI runs both with `node --check` of every scenario. There is no Playwright run of the
driver here (no protocol host can resume or select): GNOME's `live-folder` job and the
Mac self-test run it against the real writers.

## Extension reload and tab readiness

`node tests/extension-reload.mjs` uses a temporary Chromium profile and unpacked
extension with a disposable signing key, so installed native-host allowlists cannot
connect this fixture to a real app. It reloads through Chromium's extension manager, keeps the meeting
document alive, joins an 880 Hz WebRTC peer, and waits for discovery, the new
relay and non-silent PCM at a fixture native sink in the replacement worker.
It then reloads during active recording while the peer keeps sending, observes one
relay stop and one shared-context close, and records again in the same document
with no page errors. A separate case keeps a pre-handshake hook across an upgrade, checks the reload
guidance and reload action, then rejoins and becomes ready. It uses no
microphone, speaker, installed app or real Library. The 15 s bounds
limit observed worker/discovery/audio waits; there are no fixed settling sleeps.
The test runs in `npm test` and is portable to the bundled Chromium on macOS.

The VM `relay-readiness` tests exercise incompatible hooks, stale instance replies,
invalidated-relay cleanup (including exactly one stop on invalidated disconnect),
restricted injection, per-frame lost readiness and the
explicit reload action. The control schema/consumer vectors separate native
connectivity from tab readiness and require idle browsers to remain warning-free.

The page-capture unit regression holds the recorder flush on an explicit promise,
then delivers a duplicate Stop. Context closure, stopped, and replacement hookReady
must wait for that flush and occur once; the replacement can then record. This
covers the reviewed disconnect/retire ordering independently of Chromium's choice
to invalidate a relay through its next send or its disconnect callback. Control
unit tests hold recovery across notifications, stale callbacks and an old pending
snapshot; only hello may publish before recovery completes or fails. Reload-action
tests require the response to clear stale readiness and a repeated command to say
gone even before Chrome delivers its loading event.

Mixed-frame readiness regressions combine a healthy frame with an unavailable,
checking, or lost meeting frame. They verify idle inventory omission, refusal of
app and popup track Start before native creation, recovery to exactly one capture,
retained active inventory/status and busy precedence. Separate event-driven cases
change readiness during tab lookup/relay discovery and inventory lookup, and check
that an unrelated unready tab does not block a healthy one.

## Companion diagnostics

The companion-diagnostics unit tests check hello version/install metadata, both
404 file cases, empty/unreadable files, a controlled startup deadline, reconnect
caching, each terminal loss cause, first-cause preservation and normal Stops.
The unpacked-extension browser test checks Chrome's actual development install
type and resource self-check, then removes the worklet only from its disposable
copy and checks the degraded diagnostic. Schema vectors retain legacy hellos and
Stops, and tolerate unknown future cause identifiers. These are extension-side
checks; app setup wording, fallback persistence and Details rendering belong to
the consumer implementations.

The trusted-popup tab-status regression starts two independent captures and
queries each plus an idle tab through the real runtime-message handler. It covers
retained capture with degraded readiness, legacy first-session selection, pending
tab lookup/native acknowledgement/persistence, an untargeted popup Start once its
tab is known, malformed IDs, stopped-tab isolation and popup-only authorization.
Startup barriers are explicit promises/events; they introduce no settling sleeps.

The existing popup transport omits tabId from its legacy status requests (rather
than sending a null reload target); explicit null remains an invalid scoped ID.
The unpacked Chromium reload regression exercises this real popup/RPC pairing.

Popup review regressions retain rejoin guidance across observed status polls,
clear it on fresh evidence, and clear stale reload refusals on recovery or a
worker error. The real DOM test checks the dark installation warning's computed
foreground/background contrast, with a 4.5:1 floor. Poll-count and UI state waits
are event/state-based; there are no fixed settling sleeps.
