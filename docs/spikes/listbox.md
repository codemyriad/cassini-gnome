# ListBox rendering spike — fallback only, do not merge

The real-row prototype removes unrealized-height estimation and leaves native GTK scrolling intact. All six datasets had zero backward anchor frames during six native flicks paused and six with playback running/follow off; adjustment upper stayed constant after load. However, both containers needed about 33 seconds to finish the 4-hour transcript, scrolling reached 68–71 ms paint CPU, and resize reached 1.3–1.8 seconds. This is a useful fidelity/reference fallback, not the recommended shipping renderer for long meetings.

## Implementation

`CASSINI_TRANSCRIPT_RENDER=listbox` selects real `Gtk.ListBox` rows; `CASSINI_LISTBOX_CONTAINER=box` selects a plain vertical `Gtk.Box` containing the same widgets. Default application rendering is unchanged. Rows reuse MeetingPage's existing binder, speaker/timestamp column, word links, interjection markup and sidebar. Construction runs top-down in idle chunks with a 3 ms thread-CPU target. A label measurement override avoids the expensive unconstrained glyph-per-line minimum-width pass while retaining GTK's real wrapped height. Playback updates only allocated visible rows. There is no replacement kinetic controller, consumed scroll event, or synthetic height estimate.

## Measured results

Roy, headless private GNOME desktop, GTK 4.22.2, Cairo renderer, 1280×800; resize to 1000×800 and back. Source renderer commit 62990c6 (SHA receipts in JSON). One final run per case, shared host: descriptive observations, not a p95 or a controlled comparison with other agents. Page-open diagnostics start with an already decoded/cached model and exclude fixture generation/portable parsing. Paint CPU is main-thread before-paint→after-paint; interval CPU also includes work between frames. Times below are milliseconds. RSS includes the whole application, decoded model and font caches.

| Container / fixture | First / fully painted (elapsed) | Worst load paint / interval CPU | Worst scroll paint CPU paused / playing | Highlight CPU p90 / worst | Worst resize paint CPU | RSS MiB |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| listbox / 60 min named | 173 / 1002 | 64.6 / 111.5 | 17.4 / 19.7 | 0.61 / 1.62 | 439 | 168.2 |
| listbox / 4 h named | 86 / 33018 | 90.3 / 166.5 | 69.4 / 70.6 | 0.54 / 9.01 | 1264 | 354.9 |
| listbox / 60 min unassigned | 109 / 490 | 16.8 / 46.6 | 10.3 / 10.3 | 0.56 / 0.74 | 122 | 143.7 |
| box / 60 min named | 81 / 2063 | 34.2 / 60.9 | 21.7 / 20.2 | 0.55 / 2.10 | 302 | 165.9 |
| box / 4 h named | 141 / 33828 | 86.2 / 160.5 | 66.1 / 67.9 | 0.52 / 8.43 | 1850 | 348.3 |
| box / 60 min unassigned | 91 / 673 | 14.9 / 35.5 | 8.5 / 7.4 | 0.31 / 0.43 | 85 | 143.4 |

The named 60-minute fixture has 12,825 words, 490 readable rows and 162 chips. The 4-hour fixture has 52,696 words, 2,000 rows and 665 chips. The unassigned 60-minute fixture has 12,141 words and 122 paragraphs: no named roster, but one required placeholder word speaker id, which the normal model exposes as a generic speaker. It is not a zero-speaker Word schema. Fixtures contain synthetic English and silent audio only.

Every case received actual GNOME Shell/Clutter virtual-pointer FINGER events for four downward and two upward flicks in each playback state. Each of the six sent windows must contain corresponding-direction received app input; the analyzer rejects zero-input/zero-motion runs. It examines after-paint logical row/within-row anchors throughout finger-down and native coast, allowing one pixel rounding within a row and excluding intentional direction reversals. Zero backward frames were observed in all twelve phases (72 flicks total), and each phase had exactly one upper value. This covers the sampled viewport path after complete load, not every row, every device, or scrolling during initial construction.

Frame gates and FIFO acknowledgements establish completion; three unchanged after-paints establish quiescence. There are no fixed-sleep barriers or elapsed-time pass thresholds. Outer process timeouts are hang guards only. Early no-input runs are retained outside the repository and excluded. Native input can be coalesced by GTK; receipts include per-flick received counts rather than assuming every injected delta arrives separately.

## Why it does not scale well enough

Fast first paint does not mean instant access to the end: the 4-hour tail remains unbuilt for about 33 seconds. Idle callback budgeting does not budget GTK's subsequent layout/painting. Both retained-widget variants repeatedly lay out/paint a growing tree; replacing ListBox with Box did not materially solve completion or steady scrolling in this sample. Resize reflows every word and changes the logical viewport anchor: a production implementation needs one-shot logical anchor restoration after reflow. During incremental construction upper necessarily grows; bottom overshoot and live-edit anchoring are not proven safe by the completed-load result.

## D84 and interactions

The existing word-to-readable-row pipeline is unchanged. `tests/test_render_vector.py`: **159 passed**. No vector or shared strings changed. Word URI audit resolved every displayed word to its source Word (12,825 / 52,696 / 12,141). Actual GtkLabel activation checks exercised host words and an interjection, verified exact seek position and current-word highlight. Speakers/timestamps and inline chips use the existing renderer; follow-along exercised the existing navigation route and reached row 44 at five minutes in the named fixtures (row 9 unassigned). Sidebar remains the existing widget.

Search highlight/jump is not implemented as a separate spike feature: production would retain original-word→row mapping and add match markup to the same links. Navigation to an unbuilt far row currently waits for its build; prioritization or a loading state would be required. Live model splices preserve unchanged prefix widgets and rebuild the changed tail, but revised diarization, paragraph regrouping and selection preservation need regression coverage. Selection/copy is unchanged: labels do not provide cross-row text selection; a transcript copy action could use the canonical row projection, while interactive cross-row selection requires more work.

ListBox retains native list/list-item semantics and real GtkLabel links/buttons. Plain Box keeps real controls but loses ListBox's automatic list roles and row navigation. Neither has been audited with Orca; exposing 50k links may itself be costly. Keyboard, touch, scrollbar and accessibility behavior remain GTK's rather than a custom momentum implementation, but full device/a11y verification remains required. Standalone Reader is intentionally unchanged in this Library-only spike.

UI tests that depend on ListView factories/recycling need adapters: listening-rows, meeting, transcript bindings, wide-speakers, search, interjections and journey parity. Preserve semantic assertions and add native input receipt checks rather than substituting signal-only scroll tests. Packaging adds one Python module to Meson, with no new dependency, network request or runtime service.

## Recommendation and effort

**Only as fallback.** Prefer evaluating TextView or an exact-layout bounded renderer for the long-meeting workload. Their separate results use somewhat different fixtures/input methods, so this report makes no numerical head-to-head claim. GTK ListView has no demonstrated API here for supplying exact heights of every unrealized row; returning to estimates would restore the original problem.

Estimate 2–4 engineer-days for a Library/Reader adapter, semantic test migration and basic accessibility/selection work, plus unbounded additional performance work before real rows meet the 4-hour requirements. A row/width measurement cache alone does not remove retained-tree traversal cost. Do not ship this prototype as the scroll fix.

## Reproduction and evidence

Shared generator, neutral probe, native input and driver: `tools/render-spike/`. See its README. `summarize_listbox.py SHARED --output docs/spikes/listbox-measurements.json` checks receipts and produces the committed summary. Raw synthetic traces are on roy under `/mnt/ai/cassini/dev/spikes/qa-listbox/shared/final-{lb60,lb240,lbplain,box60,box240,boxplain}/`. Failed initial device-warmup measurements are excluded. Source hashes identify measured application code; subsequent changes only add metadata, analysis and this report.

RAM gate checked before desktop/large loads; heavy lock serialized measurements; app RSS peaked below 400 MiB, own desktop plus app remained below the 2.5 GiB limit. Private desktop is stopped. No Docker builds or other agents' resources were touched. #310 remains draft; D92/D88 were not resumed.

## Checked bug classes (D87)

Applicable classes: virtualized variable-height origin correction (actual native flick receipts and logical-anchor checks); idle-source lifetime/cancellation (builder removed on close); stale presentation/word identity (existing binder plus all-word URI and seek/highlight audit); UI-thread bulk work (load/scroll/resize CPU evidence, explicitly fails long-meeting suitability); shared rendering divergence (159 D84 vector tests). This is a draft research prototype, not a ready implementation; full live-update, a11y and navigation regressions remain open.
