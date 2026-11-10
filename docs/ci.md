# CI orchestration and reuse (D20)

`ci.yml` is the single PR/main/nightly/dispatch entry point. Its Changes job uses
`build-aux/ci/pr-paths.json`, a snapshot of the standalone PR coverage at f020705.
Source, packaging and Recording still run on every PR. Boards, Library vectors
and offline inference retain their existing positive path patterns; workflow/CI
infrastructure changes run every area. Renames include both old and new paths.
Missing bases, invalid events and failed diffs select everything. Main, nightly
and dispatch never use PR path filtering. Legacy UX spike push branches are kept.

Even a failed Changes job causes the build and area calls to default to running;
partially written false outputs from that failed job are never trusted.
A failed profile does not suppress other consumers of already-uploaded products;
a missing product makes its consumer fail explicitly. Root calls use
`!cancelled()` so detection failures run everything while manual cancellation
still stops downstream work, following [GitHub status-check semantics](https://docs.github.com/en/actions/reference/workflows-and-actions/expressions#status-check-functions). No automatic cancellation remains. Agents may cancel their own superseded heads
manually, never main/nightly or the last potentially mergeable head.

The PR checks list names the areas: Changed areas, Build, Meson tests, Source,
Packaging, Recording, Boards, Library and Offline. Recording runs two jobs in parallel:
`capture` and `live-folder` (shared test 2, D68), so the live folder never lengthens the
capture critical path. Both gate Recording: `live-folder` became gating at R93 (26/26
hosted passes). A failure is root-fixed (D66), never un-gated; `test_ci_hygiene.py`
refuses `continue-on-error` in every workflow. Called workflows have `workflow_call`
and `workflow_dispatch`, without duplicate PR/push/nightly entry points. Each
can still run alone; standalone dispatch builds its own required profile.

## Products and profiles

One Linux build job produces the distinct configurations without conflating them:

| Product | Configuration | Consumers |
| --- | --- | --- |
| `ci-core` | Default profile, no bundled engines, normal `/usr/local` prefix | Staged install; Recording uses its compiled resource |
| `ci-boards` | Development profile, engines disabled, `/tmp/cassini-ci-boards` | Light/dark boards and all native GUI flows |
| `ci-offline` | Default profile, `/usr`, bundled Whistle/Silero/private Python ORT and sherpa | Both engines, goldens, ABI and real queue/GTK proofs |

The source-validation environment stays an Ubuntu 26.04 container without GI.
It gains a compiler and Git to exercise ABI and diff integration tests, plus CA
certificates for verified HTTPS and zstd for the same cache format as host jobs.
The bundled profile pins the prior Ubuntu host multiarch libdir explicitly,
rather than accepting the minimal container's `lib64` fallback. Whistle and
Parakeet harnesses both mount the installed private Python ORT and assert 1.31.0;
the Whistle GTK proof imports installed modules instead of bypassing paths.json. All
original Meson/protocol/desktop/schema tests, POT and staged-install checks run.
The Meson tests and the POT check run in their own job (`meson-tests.yml`, H),
from the same checkout in the same minimal container, beside Build instead of
ahead of it: Recording, Boards and Offline wait only for the products. The
Pytest coverage gate fails unless that job passed, and `green_main.py` requires
both jobs in the main run a release comes from.
Distro and package builds remain separate: their OS/ABI/recipe tests are distinct.
No packaging recipe is changed. All six source distros, five native packages,
Flatpak and Recording scenarios remain enabled with their existing thresholds.

The pytest suite runs once per image (G11, D20). A Source distro job leaves
out only its `pytest` Meson suite when a package job runs that suite in the
same image; it still builds, runs every other Meson test, installs and
smoke-tests. The matrix names that job in `pytest-in`, and the job prints it.

| Source job | pytest suite runs in |
| --- | --- |
| debian:testing | Debian package / Build .deb (debian:testing) |
| ubuntu:26.04 | Debian package / Build .deb (ubuntu:26.04) |
| openSUSE Tumbleweed | RPM and Arch packages / Build .rpm (Tumbleweed) |
| archlinux:latest | RPM and Arch packages / Build Arch package |
| fedora:latest | itself (the rpm is built on fedora:44) |
| alpine:edge | itself (no package job) |

The deb and rpm builds have the GTK and libadwaita typelibs as check
dependencies, and every recipe runs its check with `CASSINI_TEST_REQUIRE_GTK=1`.
pytest then stops with a usage error if a typelib is missing, instead of
skipping the GTK application tests that only the Source job used to run there.

Those package jobs are required: the debian:testing build no longer has
`continue-on-error` (a failure there already blocked a release in
`green_main.py`). After each build, `build-aux/ci/check_pytest_ran.py` proves
from its Meson logs what pytest ran: pytest-1 .. pytest-N each once and passed,
with the command `src/meson.build` registers, their shard,
`CASSINI_TEST_REQUIRE_GTK=1` and no `PYTEST_ADDOPTS`; each shard read the
source's `pyproject.toml`, deselected nothing and tested a `tests/` tree
identical to the checkout (`tests/conftest.py` records these per shard);
every test file is in the shard `tests/shard_plan.py` gives it; and no skip
falls outside its short `SKIPS` list. The job uploads its node ids and the
os-release of the container that ran them as `pytest-ran-<image>`.

Every Source job records from its own logs either the node ids its pytest ran
or, whatever made it skip, `pytest-skipped-<image>`. CI's **Pytest coverage**
job then fails if a skipped image has no `pytest-ran-` proof from a container
of the same release, or if any package job ran fewer cases of a test function
than the fedora:latest Source job, the reference that keeps the whole suite
with GTK outside a package build. A test left out only in package builds, by
a deselection, a conditional definition, a patch or another image, fails here.
`tests/test_ci_pytest_replacement.py` also pins the recipes' test commands,
hooks, patches and check dependencies, the job conditions and the gate, and
mutates each (every bypass found in two adversarial reviews among them) to
prove the guard catches it.

Tar products preserve executable bits. Before use, consumers check their exact
GitHub commit, profile and every file digest; extraction rejects traversal, links
and devices. The board install uses its configured fixed prefix and compiled
schema; moving a launcher to a different prefix is never assumed to work.
Recording and offline consumers do not rebuild products. Build and package jobs
share the pin-keyed `.whistle-cache` (including private ORT wheels); native sherpa
uses `.sherpa-cache`. The model fixture cache is revision-keyed, test-only, and
prepared separately from runtime build inputs. Meson stays offline.

The installed-package harnesses (Desktop, build refresh, browser setup) build
in two stages (`tests/harness-image.sh`, C). The harness stage holds the Ubuntu
base, the harness's own packages and the static `Depends` of `debian/control`.
Its base is the image the Dockerfile's `ARG BASE` names, pinned to the digest
of the runner platform's manifest (`tests/harness-image.sh pin`/`base`, one
Docker Hub pull: a single GET of the raw index): the harness stage is built
`FROM` that pin, never the bare tag, and a base that cannot be resolved fails
the run. `CASSINI_HARNESS_BASES=IMAGE@sha256:...` (the `linux/ARCH` entry of
`docker buildx imagetools inspect --raw IMAGE`) only skips that lookup, for
tests or Docker Hub's rate limit; the build still pulls the digest and every run
still runs `apt-get`, so neither is offline. A `CASSINI_BROWSER_TEST_BASE` that
names its own digest is resolved itself, never replaced by a pin of the same image. The stage is tagged with a hash of every input, the pinned base and
the ISO week, and rebuilt with `--pull` whenever that tag is new, so it never
outlives its Dockerfile, the control file, its base or a week. The cache only
saves download and unpack time, never freshness (D68): every run first runs
`apt-get update` and `apt-get dist-upgrade`, so the base and the app's runtime libraries in the cached stage
(GTK, libadwaita, GStreamer, PyGObject) are as current as an uncached build's,
then installs app.deb, whose own dependencies apt resolves against the same
indexes. That per-run stage is built with `--no-cache` and the tests still run
with `--network=none`. The offline worker image (`tests/whistle/Containerfile`)
is tagged, rebuilt and brought up to the current archive the same way. CI
keeps the harness stages in a cache that main saves and pull requests only
restore (`build-aux/ci/harness-cache.sh`, D66); a miss just builds them. The
cache key's step resolves each base once and pins it for the whole job
(`$GITHUB_ENV`), so the key, the restored tag and a fresh build name the same
base even if the registry moves it mid-job. The products under test and the
package builds' distro images are never cached.

## Releases

Release downloads the five Debian/RPM/Arch products the exact main commit
built and tested (no rebuild at tag time). `green_main.py` requires the latest
main push orchestrator to succeed, every nested job green, all six package
artifacts (the stable Flatpak's among them, as proof main built and
smoke-tested it) and all three full-run build products present and unexpired.
A PR, another commit, a failed run or a partial/expired artifact set cannot
supply a release. No tag or publication is created to validate this wiring.
`publish` needs exactly the Debian and RPM/Arch jobs and creates the GitHub
release with those packages and the tagged version's NEWS section
(`build-aux/ci/release_notes.py`).

Linux ships the deb, rpm and Arch packages only; no Flatpak is published
anywhere (D45.4, 2026-11-05, replacing D45.3's Flathub plan, which Flathub's
generative-AI policy rules out). We run no Flatpak repository and hold no
signing key, and a release never publishes or attaches a Flatpak bundle. This
replaces G15's guard, which refused to ship the bundle until our own signed
D45.1 repository served it. Main's stable Flatpak is still built without an
update remote, validated and smoke-tested in `flatpak.yml` below, as a build
check; it is a test package from main only.

Every PR checks the version across Meson, NEWS, metainfo, the man page and all
native recipes, in Lint (`source.yml`). `flatpak.yml` builds the stable Flatpak,
validates its exported desktop/AppStream files and launcher version in the SDK,
then installs it and runs the normal offline smoke checks, the Whistle and
Parakeet goldens included. The devel bundle the same job builds first runs
every check but those golden decodes (`smoke.sh --without-goldens`, F): its
engines come from the same pinned sources, it still verifies their installed
payload offline, it imports, uses and hash-checks its NumPy against the wheel's
RECORD (its NumPy stage comes from the flatpak-builder cache, the stable
bundle's is built fresh), and its manifest differs from the stable one only in the ID
and `-Dprofile`, which no recognition code reads (`tests/test_ci_flatpak_dedup.py`
fails on a new branch on the profile). The job's host Trash portal (fuse and
xdg-desktop-portal, compiled by `tests/flatpak/prepare_trash_portal.py`) comes
from a cache main saves and pull requests only restore, keyed on that script,
the job's container image and the runner; the script rebuilds unless the
restored tree names the same pins and prefix. The tag workflow
rejects a tag that disagrees with any package version. `release-check.yml` is
dispatch-only: run it on a release branch to check its metadata before tagging.
Native package scripts validate the installed metadata and launcher after the
package-manager install, in addition to their unchanged clean-container offline
smoke tests. No validator error is suppressed.

A release PR (`release/X.Y.Z`) bumps every version that check reads and adds the
NEWS section, which the tag workflow publishes as the release notes
(`build-aux/ci/release_notes.py`, ahead of GitHub's generated PR list). After it
merges and main's run is green, the tag `vX.Y.Z` on that merge builds nothing new:
it publishes what main tested.

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


APT network acquisition in CI uses 15-second HTTP/HTTPS timeouts and three
retries, including both cached harness stages and their current-archive refresh.
Index updates use `APT::Update::Error-Mode=any`: an unavailable repository fails
the update rather than silently keeping incomplete or stale indexes. Changing a
harness Dockerfile also changes its cache key, so existing cached images cannot
bypass these defaults. The bounded install helper retains its separate whole
network-phase budgets and two attempts (one internal acquisition retry); package
configuration runs once, without network or retries. Test/job timeouts are unchanged.

The offline recognition job refreshes its harness against the current archive
once, then shares that immutable image across Whistle, Parakeet, D24 and D25.
Package-refresh progress is visible before the network-isolated tests start.
The image is labelled with this job's identity; other jobs cannot reuse it, and
cleanup checks ownership before removing that exact image. It is never saved
in the weekly harness cache. Standalone run.sh invocations still refresh once
per invocation. All original offline checks and resource limits remain active.
