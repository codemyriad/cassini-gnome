# CI coverage and reuse (D20)

Standalone PR entry points and their path coverage stay in place. There is no
central orchestrator. Source builds for six distributions, distro packages,
development-profile boards and the installed offline-engine build have different
configuration/runtime requirements; they are not interchangeable app products.
The offline job builds its installed stage once and uses it for both engines,
the actual queue and all the golden/GTK proofs.

All five check workflows now run on main, nightly and dispatch, including the
full package and distro matrices via CI's reusable workflows. Existing PR path
filters are retained and widened for schemas, Meson and CI helpers. The original
per-PR cancellation rule remains; main, nightly and dispatch have distinct groups
and are never automatically cancelled. Agents may manually cancel only their own
superseded heads, never the last potentially mergeable head or a main/nightly run.

Whistle/Needle/Silero and every pinned private Python ONNX Runtime wheel share
the `.whistle-cache` location and pin-derived key across workflows. Native sherpa
uses `.sherpa-cache`, keyed by its exact pins. Meson remains offline; each consumer
checks the bytes. The Parakeet test-only weight cache stays keyed to its revision.
Running the ML workflow on main makes those verified caches available to later PRs,
rather than leaving successful PR-only caches isolated on pull-request refs.

## Exact main packages, reused at release

Debian, RPM and Arch release products are identical to the five packages already
built, installed and smoke-tested on main. Release downloads those artifacts
instead of building them again. `green_main.py` requires the exact tag commit's
latest main push run of every check workflow to have succeeded, every job to be
green (including continue-on-error jobs), and all five artifacts to be unexpired.
A PR, another branch, an older commit, a previous successful attempt hidden by a
new failure or a missing/expired artifact cannot supply a release.

The release Flatpak remains a separate build: main produces the development
profile, while a tag ships the default profile. No packaging recipes are changed.
No tag or release is created to test this plumbing. The helper's rejection rules
are unit tested; artifact download uses GitHub's digest-checking action. A real
release still needs main to finish before the tag is pushed.

## Measurements

Use `python3 build-aux/ci/measure_ci.py FULL_SHA --output report.json` with a
read-only authenticated `gh`. It records every latest PR workflow attempt and
job, execution duration and creation-to-start queue wait. Rounded Linux minutes
are an estimate (each job rounds separately), not a billing invoice. Pending jobs
remain explicitly incomplete. Compare complete, successful heads, and retain
reports rather than attributing runner/network variance to a cache change.

The earlier engine PR #50 is retained as historical context in
`ci/engine-baseline-pr50.json`: 18 jobs, 46.667 execution minutes, 55 estimated
rounded Linux minutes, median 3 seconds queue wait. The model-selection PR #51
is the closer before control: `ci/before-pr51.json` records 17 jobs, 47.95
execution minutes, 56 estimated rounded Linux minutes and median 5 seconds queue
wait. This PR's after report will be recorded after all checks finish. Per-PR job count is not intentionally
reduced: the added paths can run an additional vector check. The guaranteed build
saving is at release (five existing package products reused); cache benefits on
PRs depend on main warming and current runner/network conditions.
