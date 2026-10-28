# CI orchestration and reuse (D20)

`ci.yml` is the single PR/main/nightly/dispatch entry point. Its Changes job uses
`build-aux/ci/pr-paths.json`, a snapshot of the standalone PR coverage at f020705.
Source, packaging and Recording still run on every PR. Boards, Library vectors
and offline inference retain their existing positive path patterns; workflow/CI
infrastructure changes run every area. Renames include both old and new paths.
Missing bases, invalid events and failed diffs select everything. Main, nightly
and dispatch never use PR path filtering. Legacy UX spike push branches are kept.

Even a failed Changes job causes the build and area calls to default to running.
No automatic cancellation remains. Agents may cancel their own superseded heads
manually, never main/nightly or the last potentially mergeable head.

The PR checks list names the areas: Changed areas, Build, Source, Packaging,
Recording, Boards, Library and Offline. Called workflows have `workflow_call`
and `workflow_dispatch`, without duplicate PR/push/nightly entry points. Each
can still run alone; standalone dispatch builds its own required profile.

## Products and profiles

One Linux build job produces the distinct configurations without conflating them:

| Product | Configuration | Consumers |
| --- | --- | --- |
| `ci-core` | Default profile, no bundled engines, normal `/usr/local` prefix | Meson validation/install/POT; Recording uses its compiled resource |
| `ci-boards` | Development profile, engines disabled, `/tmp/cassini-ci-boards` | Light/dark boards and all native GUI flows |
| `ci-offline` | Default profile, `/usr`, bundled Whistle/Silero/private Python ORT and sherpa | Both engines, goldens, ABI and real queue/GTK proofs |

The source-validation environment stays an Ubuntu 26.04 container without GI.
It gains a compiler and Git to exercise ABI and diff integration tests. All
original Meson/protocol/desktop/schema tests, POT and staged-install checks run.
Distro and package builds remain separate: their OS/ABI/recipe tests are distinct.
No packaging recipe is changed. All six source distros, five native packages,
Flatpak and Recording scenarios remain enabled with their existing thresholds.

Tar products preserve executable bits. Before use, consumers check their exact
GitHub commit, profile and every file digest; extraction rejects traversal, links
and devices. The board install uses its configured fixed prefix and compiled
schema; moving a launcher to a different prefix is never assumed to work.
Recording and offline consumers do not rebuild products. Build and package jobs
share the pin-keyed `.whistle-cache` (including private ORT wheels); native sherpa
uses `.sherpa-cache`. The model fixture cache is revision-keyed, test-only, and
prepared separately from runtime build inputs. Meson stays offline.

## Releases

Release downloads the five Debian/RPM/Arch products already installed and
smoke-tested by the exact main commit. `green_main.py` requires the latest main
push orchestrator to succeed, every nested job green, all five package artifacts
and all three full-run build products present and unexpired. A PR, another commit,
a failed run or a partial/expired artifact set cannot supply a release. The
release Flatpak is a separate default-profile build; main tests development.
No tag or publication is created to validate this wiring.

## Measurements and validation

`python3 build-aux/ci/measure_ci.py FULL_SHA --output report.json` records latest
PR attempts, every job's execution and creation-to-start queue wait, and GitHub's
reported billable usage. Per-job rounded Linux minutes are a separate resource
estimate, not a billing invoice. Pending jobs stay explicitly incomplete.

The closest standalone-workflow control is #53's final 011c693, retained in
`ci/before-orchestrator-pr53.json`: 18 jobs, 45.217 execution minutes, 54 estimated
rounded Linux minutes, 0 GitHub-reported billable minutes, median 148 seconds queue.
Earlier controls (#50 and #51) remain in `ci/` for context. The orchestrator's
final-head comparison is written to coordination `STATUS-GNOME.md` after checks
finish. Queue and network differences are reported without claiming causality.

Tests cover hand-written area expectations, unknown history, invalid event files,
actual Git renames, wrong-commit/profile products, corrupt digests, traversal and
release provenance/expiration. Local restore proofs run the real native GTK/audio,
light/dark boards and installed offline goldens from the archived products. Hosted
CI checks artifact transfer and every matrix/profile again, then main verifies the
full unfiltered graph. Standalone dispatch remains independently available.
