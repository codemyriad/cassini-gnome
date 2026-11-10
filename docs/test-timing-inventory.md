# D88 timing audit

Baseline: main `1acfafd` (2026-11-10). This inventory scans all 415 tracked test
source files, including the pinned `browser-protocol` test copy. It records
1,211 timing-related sites in 217 files: 367 clock reads, 70 fixed-delay
references, 89 timer registrations, 51 direct clock conditions, 65 candidate
temporal assertions and 569 bounded waits. GNOME owns 1,114 sites; 97 are in
the vendored protocol copy.

[The CSV](test-timing-inventory.csv) gives file, baseline line, category,
ownership and expression. A site can occur in several categories. These are
conservative audit candidates, **not 1,211 confirmed violations**. In particular,
media timestamps, injected clock values, diagnostic-only timestamps, memory
budgets and generous outer hang guards must keep their domain coverage.
`review` means the area's migration must classify the site before changing it.

Regenerate with `python3 build-aux/ci/inventory_test_timing.py`. It scans tracked
sources (no build products or dependencies), recognizes Python clock aliases,
propagates clock-dependent values into comparisons and checks JavaScript,
Python, shell, Kotlin, C and HTML timer/clock references. Indirect helper calls
must also be followed during each area's review; this source scan is not a
formal proof that every timing dependency has been identified. The CSV is a
baseline migration map; later PRs report which sites they convert or retain.

## Conversion order

| Area | Evidence / sites | Conversion and retained purpose |
|---|---|---|
| Quit / background completion | Boards job 129608906461, `tests/ui/quit.py` | #299: release worker after GTK observes the held lifecycle; direct-close work uses main-thread CPU; wall timestamps are diagnostic; parent watchdog only guards hangs. |
| Companion open/close frames | Boards 129208180375 (101.9 ms), 129249830836 (124 ms), 129387060148 (108 ms); `tests/ui/companion-click.py` | Thread CPU between dispatched frames, preserving the work budget; paint/unmap/worker events replace two-second settling waits. |
| Recording-page observation | Boards 129623954036, `tests/ui/recording.py` and comfort tests | Observe page/source/worker completion events; preserve forced stale-snapshot orderings and notification assertions. #300 fixes the lost-page/state bug; its wait helper still needs migration. |
| Long transcript / render scaling | `tests/test_render_vector.py`, transcript suites and subprocess limits | Separate hang guards from pass budgets; measure thread CPU or 2N/N scaling with output coverage. |
| Browser / native controls | Short 0.05–1.5 s assertions in connection, companion, recorder, native recorder, sources, model controls and transcription tests | Gate held worker operations and prove the caller returns before release; use injected clocks for protocol expiry. |
| GUI polling helpers | `tests/ui/*`, desktop and installed journeys | Signals, frame-clock events, process exits and futures. Retain one generous outer process watchdog. |
| Capture clocks and sample positions | `tests/test_session_clock.py`, tap stall, recording comfort, live playback | Inject time at the clock boundary; retain exact media-position/clock-origin contracts. Do not delete sample-duration assertions. |
| Download / file / async workers | model, library-monitor, transcription and open/load tests | Explicit thread/future/transfer events; retain length/hash, atomic publication, cancellation and saved-state checks. |
| Harness timeout contracts | `tests/test_bounded_run.py`; pending #302's new helper test | Deterministically inject timeout/cancellation outcomes. Real Docker cancellation probes remain external evidence, not wall-time pass conditions in unit tests. |
| Vendored protocol tests | CSV owner `vendored` | Report to the protocol owner and consume an upstream update, preserving the pinned copy's integrity. |

The previous header-refresh flake is already covered by dispatched input work
and the 16-header batch cap (`tests/ui/listening-rows.py`, “Header refresh
monopolized the main loop”). Keep that event/work-count regression. It is not a
wall-clock budget and should not be removed merely because of its wording.

D87 applies to every migration PR: list the applicable bug classes and tests in
its body, use a main base, and mark ready only after exact-head CI is green.
