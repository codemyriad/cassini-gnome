# Cassini browser recording protocol

Cassini records online meetings with each participant on their own audio track. The only
place where those tracks still exist separately is inside the browser: once the audio
reaches the speakers it is one mix, and the operating system cannot tell who said what.
So the recording has to start in the browser. Separation still depends on the sources the
meeting exposes: a mixed receiver remains mixed, and a track is not a verified participant.

This repository is the code that does that, shared between
[Cassini for GNOME](https://github.com/codemyriad/cassini-gnome) and
[Cassini for Mac](https://github.com/codemyriad/cassini-mac) (Windows later). Each app has
its own native recorder, written in Python and Swift respectively. What they have in common
is the browser side, and the agreement on how browser and recorder talk to each other. That
common part lives here, so there is one copy of it instead of two drifting ones.

## how it works

* `extension/` is a Chromium extension (Manifest V3). On a meeting page it hooks
  `RTCPeerConnection` early, finds the audio tracks the page sends and receives, and
  turns each one into timestamped PCM with an `AudioWorklet`. When the page hides its
  tracks it falls back to recording the whole tab through
  [`chrome.tabCapture`](https://developer.chrome.com/docs/extensions/reference/api/tabCapture).
* The extension streams that audio to a small local program through
  [Chrome native messaging](https://developer.chrome.com/docs/extensions/develop/concepts/native-messaging)
  (length-prefixed JSON on stdin/stdout, nothing on the network). Each app ships its own
  such program; both answer to the name `com.codemyriad.cassini`.
* `SPEC.md` is the contract between the two: the messages, their types and limits, how
  timestamps and gaps are handled, and the on-disk result (a folder with one WAV per source
  and a `session.json`). `schemas/` holds the same thing as JSON Schema.
* `conformance/` keeps the two implementations honest. `vectors.json` is a list of raw
  request sequences with the responses and files each one must produce; `run.py` pipes
  them into any host executable. A host that passes the vectors works with the extension.
  Error messages are deliberately not part of the contract, only error codes.
* `scenarios/` has the audio helpers the browser tests in both apps share (tone
  generation and spectral power checks), so a test that proves "the 880 Hz source
  did not bleed into the 440 Hz one" reads the same on both platforms.

A few things that are easy to get wrong, and that the spec and vectors pin down: a track is
a source, not a person (Google Meet, for one, multiplexes speakers over three receiver
slots); timestamps come from the audio render clock, not from when a message arrived;
a recording that gets interrupted must still leave playable files behind.

## how the apps use it

Each app vendors this repository at a pinned commit under `browser-protocol/` and keeps its
installed extension byte-identical to `extension/`. `verify.py` checks that:

```sh
python3 browser-protocol/verify.py --pinned BrowserExtension     # Mac
python3 browser-protocol/verify.py --pinned browser-extension    # GNOME
```

Install the test-only Python dependencies, then run the request/output vectors against a host:

```sh
python3 -m pip install -r requirements-test.txt
python3 conformance/run.py /absolute/path/to/native-host
```

The host must speak native-messaging stdio and answer `start` with the folder it created.
The runner removes only folders it caused to exist; on failure it keeps them and prints
their paths. The unit tests for the extension's worklet and clock calibration are plain
Node:

```sh
node --test extension/tests/*.test.cjs
```

Browser-level tests (real Chromium, real WebRTC, real native messaging) live in each app
for now, because launching a browser with an extension and an audio device is where the
platforms differ. `TESTING.md` describes the layers and what each one does and does not
prove.

## changing things

Changes to the protocol, the schemas, the vectors or the extension land here first, with a
vector for any new rule. Then each app bumps its pinned commit. A host change that makes a
vector fail is a protocol change, not a bug fix, and gets discussed as one. The wire
version stays at 1 as long as additions are optional and old hosts keep working; `SPEC.md`
has the changelog.

Most of this code was written by coding agents (one per app, plus a reviewer), steered and
read by a human but not proof-read line by line. The vectors and the browser tests are the
evidence that it works; a gap in them is a bug worth reporting.


## SPEC v1.1 and native sessions

SPEC v1.1 amends wire/session version 1 additively. Both native recorders use the same
one-WAV-per-source folder: `source: "macos"` and `source: "gnome"`, with microphone and
system/application audio kept separate. `conformance/native-session/` contains complete
synthetic WAV files and both native manifest variants, checked by
`python3 conformance/check-native-fixture.py`.

Chromium is the current target, with Chrome 116 as the minimum. Product priorities are Teams
web, Zoom web, then Meet. `products/` contains account-based manual checklists and a result
template. `tests/product-topologies.mjs` tests controlled mixed-receiver, PC-free and
switching-slot models; their results do not establish live product/version compatibility.
Playwright is pinned to 1.61.1. `npm ci`, `npx playwright install chromium`, and `npm test`
run the unit/topology checks. Firefox and Flatpak remain future work.

## updating a vendored copy

After staging upstream edits, `python3 refresh-hashes.py` refreshes SHA256SUMS. Validation,
commit and push happen before either app updates. From this clone:

```sh
python3 vendor.py FULL_COMMIT_SHA /path/to/app/browser-protocol /path/to/app/BrowserExtension
```

GNOME's installed path is `browser-extension`. The vendor tool records PINNED_COMMIT and
UPSTREAM_COMMIT, the raw upstream Git commit object. `verify.py --pinned` reconstructs the
commit SHA and full Git tree from the vendored files, including SHA256SUMS. No private-repo
network credentials are needed in app CI. A changed pin or a locally edited file plus an
updated checksum fails this check. The proof files are app metadata, not upstream sources.

The Mac repository owns the approved DrMeyers native runner for manual/nightly jobs only.
Runner registration and synthetic fixtures do not grant macOS audio consent or replace
hardware validation. Live product testing is manual and requires test accounts.

The runner validates complete response/session schemas, derives track metadata and exact PCM
(including gap silence) from accepted requests, checks WAV fmt/sample bytes and returned paths,
and requires error termination without waiting for EOF. Corrupt-host negative tests are a
mandatory CI gate. Vector counts alone from older snapshots are framing smoke evidence.

The shared `npm test` also loads the extension and kills its worker after five seconds of
saved generated audio, in Separate tracks and Whole tab modes. It checks retained tone,
interruption/path, cleared badge and closed offscreen capture. CI uses an isolated PulseAudio
null sink and a small checked-in browser-test writer (not either production recorder).
Either app runs the same adapter against its real host with
`CASSINI_NATIVE_HOST=/absolute/executable CASSINI_TEST_EXTENSION=/absolute/extension node tests/worker-recovery.mjs`.
`CASSINI_WORKER_TEST_MODES=tracks` runs the device-independent portion; Whole tab needs an
output device and stays on DrMeyers rather than hosted macOS. The Mac loaded-extension and
tab adapters also call `scenarios/worker-recovery.mjs`; GNOME adopts this adapter with its pin.
