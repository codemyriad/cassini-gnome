# D88 timing audit

Baseline: main `3b6b9e5` (merged #306–#308, 2026-11-10), plus this PR's scanner
regression fixtures. The inventory scans 518 tracked test source files, including
both pinned extension copies, protocol scenarios and conformance runners. It records
2,261 timing-related sites in 247 files: 437 clock reads, 106 fixed-delay
references, 109 timer registrations, 97 clock conditions, 941 candidate temporal
assertions and 571 bounded waits. GNOME owns 1,670 sites; 591 are vendored.

[The CSV](test-timing-inventory.csv) gives file, source line, category,
ownership and expression. A site can occur in several categories. These are
conservative audit candidates, **not 2,261 confirmed violations**. In particular,
media timestamps, injected clock values, CPU budgets, diagnostic timestamps and
outer hang guards retain their coverage. `review` means the area's migration
must classify the site before changing it. Both `browser-extension/` and
`browser-protocol/` are pinned copies: fixes there belong to the protocol owner.

Regenerate with `python3 build-aux/ci/inventory_test_timing.py`. Only tracked
sources are considered; dependency directories and build products are excluded.
The path filter includes `.cjs` test/support files, shared scenarios and protocol
conformance runners as well as the usual test directories.

Python AST analysis resolves imports, assigned clock/sleep aliases and default
arguments, including thread/process CPU clocks. A worklist propagates timing
values through assignments and container mutations into assertions and compared
`if`/`while` conditions. Timed object fields retain their identity instead of
making every other field on that object a clock value.

JavaScript/MJS/CJS use a dependency-free lexical scan with balanced expressions:
clock-dependent assignments, destructured bindings, indexed container mutations and `for ... of`
bindings feed assertion and condition candidates, including multiline callback
results. Promise/setTimeout delays are also tagged as fixed delays. Other
languages retain direct line-pattern discovery.

This is a discovery map, not a parser/type checker or a proof of completeness.
Names are conservatively merged across scopes, so unrelated uses can be flagged.
Dynamic properties, imported helper implementations, arbitrary function-return
flows and embedded template expressions require manual follow-up. Regex/string
contents can also appear as direct-pattern candidates. The regression suite
(`tests/test_timing_inventory.py`) checks synthetic flows, non-timing numeric
checks and the concrete omissions from the review against actual source files. The
converted AT-SPI parent deadline is retained as a historical source snippet so
later D88 conversions do not have to keep the old timing code alive.
Later migration PRs report which candidates they convert or retain.

## Conversion order

| Area | Evidence / sites | Conversion and retained purpose |
|---|---|---|
| Quit / background completion | Boards job 129608906461, `tests/ui/quit.py` | #299: release worker after GTK observes the held lifecycle; direct-close work uses main-thread CPU; wall timestamps are diagnostic; parent watchdog only guards hangs. |
| Companion open/close frames | Boards 129208180375 (101.9 ms), 129249830836 (124 ms), 129387060148 (108 ms); `tests/ui/companion-click.py` | #305: thread CPU per action/dispatch; paint/unmap/worker events replace settling waits. |
| Recording-page observation | Boards 129623954036, `tests/ui/recording.py` and comfort tests | Observe page/source/worker completion events; preserve forced stale-snapshot orderings and notification assertions. #300 fixes the lost-page/state bug and converts its GUI helper to event dispatch with injected native clocks. |
| Long transcript / render scaling | `tests/test_render_vector.py`, transcript suites and subprocess limits | Separate hang guards from pass budgets; measure thread CPU or 2N/N scaling with output coverage. |
| Browser / native controls | Short 0.05–1.5 s assertions in connection, companion, recorder, native recorder, sources, model controls and transcription tests | Gate held worker operations and prove the caller returns before release; use injected clocks for protocol expiry. |
| GUI polling helpers | `tests/ui/*`, desktop and installed journeys | Signals, frame-clock events, process exits and futures. Retain one generous outer process watchdog. |
| Capture clocks and sample positions | `tests/test_session_clock.py`, tap stall, recording comfort, live playback | Inject time at the clock boundary; retain exact media-position/clock-origin contracts. Do not delete sample-duration assertions. |
| Download / file / async workers | model, library-monitor, transcription and open/load tests | Explicit thread/future/transfer events; retain length/hash, atomic publication, cancellation and saved-state checks. |
| Harness timeout contracts | `tests/test_bounded_run.py`; #302's build-wrapper tests | Deterministically inject timeout/cancellation outcomes. Real Docker cancellation probes remain external evidence, not wall-time pass conditions in unit tests. |
| Vendored protocol tests | CSV owner `vendored` | Report to the protocol owner and consume an upstream update, preserving the pinned copy's integrity. |

The previous header-refresh flake is already covered by dispatched input work
and the 16-header batch cap (`tests/ui/listening-rows.py`, “Header refresh
monopolized the main loop”). Keep that event/work-count regression. It is not a
wall-clock budget and should not be removed merely because of its wording.

D87 applies to every migration PR: list the applicable bug classes and tests in
its body, use a main base, and mark ready only after exact-head CI is green.
