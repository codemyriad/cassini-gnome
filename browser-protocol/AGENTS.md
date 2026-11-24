# AGENTS.md — working on cassini-browser-protocol

Shared browser side of Cassini: the Chromium extension, the recording and control
contracts, and the conformance vectors that the GNOME and Mac apps vendor at a pinned
commit. Cross-app UX contracts, strings, brand tokens and the review checklist live in
[`codemyriad/cassini-ux`](https://github.com/codemyriad/cassini-ux) (`AGENTS.md`,
`review/BUG-CLASSES.md`); read those first. The [README](README.md) has the repository
layout.

## Contracts

* `SPEC.md` (recording) and `SPEC-CONTROL.md` (control host) are controlling. A rule
  change lands with a vector in `conformance/` and, where it applies, a schema in
  `schemas/`. A host change that makes a vector fail is a protocol change.
* `docs/history/` and `proofs/` are context, never contract.
* `ux/` and `extension/brand/saturn-nav.svg` are vendored from cassini-ux and pinned by
  `ux/UPSTREAM.json`. Never edit them here; see the README's brand section.

## Before pushing

```sh
python3 refresh-hashes.py               # every tracked-file change updates SHA256SUMS (CI diffs it)
python3 verify.py
python3 -m pip install -r requirements-test.txt
python3 -m unittest discover -s tests -p "test_*.py"
python3 conformance/check-native-fixture.py && python3 conformance/check-session-events.py
node --test tests/event-value.test.mjs tests/rtc-topology-probe.test.mjs extension/tests/*.test.cjs
npm ci && npx playwright install chromium && npm test   # Chromium checks; CI adds a private PulseAudio sink
```

Run `refresh-hashes.py` after `git add`/`git rm`: it hashes `git ls-files`. `TESTING.md`
explains each layer and what it does and does not prove; the real Nextcloud Talk gate
runs only in CI (`.github/workflows/ci.yml`).

## Conventions

* User-visible extension changes bump `extension/manifest.json` `version` and name it in
  the PR title, e.g. `(0.2.37)`.
* Tests stay deterministic and offline: waits are on events with a failure-only bound,
  no live sites or accounts, no captured media in artifacts.
* Diagnostics and copied details never include URLs, titles, identifiers, paths or audio.
