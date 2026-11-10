# Custom transcript rendering spike — do not merge

Verdict: **only as fallback**. Exact measured heights remove GtkListView’s estimate/rebase failure while preserving native GTK kinetics and the existing GtkLabel word links. The prototype pays for that certainty with a synchronous full layout at open and at a new width. The measured four-hour layout/resize stalls are too large for production. Prefer the simpler nonvirtualized approach if its measurements and accessibility hold up.

## Implementation

Set `CASSINI_TRANSCRIPT_RENDER=custom` to change the Library Meeting page only. The standalone reader and the default ListView path retain their renderers. `ExactTranscript` implements `Gtk.Scrollable`, publishes a pixel prefix-sum index, binary-searches its visible range, and allocates real existing row widgets at those indexed heights. One viewport of overscan on either side bounds live row widgets. Scroll position never depends on which rows are realized. No input controller, deceleration implementation, or kinetic-scrolling setting is replaced. Navigation during an actively running native coast still needs an explicit cancellation policy; the existing hand-scroll follow cooldown normally prevents automatic navigation then.

One reusable probe row measures all turns using the same headers, markup, CSS ancestry and GtkLabel/Pango height-for-width as the live rows. PR #310’s `TranscriptLabel` bounded measurement cache and exact-wrapping check are reused; its scroll controller is not. Three width-specific prefix arrays are retained. Transcript/header/dark-theme/font/DPI/scale changes invalidate geometry. Arbitrary application CSS-provider metric changes still need a complete invalidation audit before adoption.

Speaker names, timestamps, inline named interjections, individual host/aside word links, current-word markup and existing follow routing are retained. The speakers pane code is unchanged. The fixture tests resolve every timed word URI and exercise actual GtkLabel activation, active-word markup and follow scrolling. The no-speakers case removes diarization/roster from the hour fixture: it coalesces to 127 reading paragraphs, still using the existing generic name/time header presentation.

## Measurements

| Meeting | Words / rows | Reverse frames paused / playing | `upper` changes paused / playing | Observed frames paused / playing |
|---|---:|---:|---:|---:|
| 60min-diarized | 12,803 / 484 | 0 / 0 | 0 / 0 | 941 / 938 |
| 4h-diarized | 51,212 / 1,933 | 0 / 0 | 0 / 0 | 939 / 939 |
| 60min-no-speakers | 12,803 / 127 | 0 / 0 | 0 / 0 | 937 / 942 |

| Meeting | First paint CPU ms | Full index CPU ms | Worst load frame CPU ms | Highlight mean / max CPU ms | Worst scroll frame CPU ms paused / playing | Worst resize frame CPU ms | App RSS MiB |
|---|---:|---:|---:|---:|---:|---:|---:|
| 60min-diarized | 397.9 | 379.5 | 368.7 | 0.115 / 0.815 | 5.62 / 4.90 | 349.1 | 146.9 |
| 4h-diarized | 768.2 | 755.8 | 741.1 | 0.106 / 1.294 | 5.38 / 4.67 | 714.1 | 158.9 |
| 60min-no-speakers | 163.0 | 155.0 | 153.8 | 0.120 / 0.928 | 4.25 / 3.94 | 153.8 | 160.4 |

Full index completion precedes first paint. All six mode/fixture combinations passed the scroll checks; each fixture then passed word activation/follow checks after its coast settled. Allocated content widths during resize were 788 → 868 → 788 px; final live row counts were 7, 7 and 4 respectively. Every observed adjustment upper remained constant during each scroll run (68,685 / 274,515 / 49,083 px). Across modes, worst frame-clock intervals were 18.591 ms for the hour meeting and 33.332 ms for the four-hour and plain cases. These intervals include scheduler/compositor effects; CPU figures isolate the UI thread.

Machine-readable summary: [render-custom-measurements.json](render-custom-measurements.json). Raw per-frame anchor samples remain at `/mnt/ai/cassini/dev/spikes/custom-qa/shared/results-final.json` on roy and are reproducible with the harness below.

Environment: private bwrap desktop from `/mnt/ai/cassini/qa/rootfs`, headless GNOME Shell/Mutter 50.1, GTK 4.22, software Cairo renderer, 1280×800 monitor. Synthetic English silence-only portable Opus fixtures; no private recordings were opened, copied or committed. Fixture generation and model parsing occur before the measured page open. All three models coexist, so app RSS includes fixture/model retention and is not renderer-only memory. Figures below are one run on the shared host, not statistically controlled benchmark results.

Open timings are UI-thread CPU from `open_meeting` to first painted mapped rows and to completion of the full prefix index. Frame CPU uses frame-clock `before-paint`/`after-paint` and `thread_time_ns`, excluding compositor/render-worker CPU. Resize waits for a real allocated-width change, tests +80 px and return, and validates the observed widths. Returning to a cached width avoids reindexing. Highlight timing measures 200 complete page `_position` updates, including transport/sidebar navigation bookkeeping, with position notification excluded from that timing.

Each fixture/mode uses six flicks: three down and three up, five finger deltas each, followed by GTK’s own kinetic ticker. Anchor observations are actual visible row bounds, not merely adjustment monotonicity (1 px tolerance within the same row; any row-index reversal counts); visible row natural heights are also checked against indexed heights every sample. Start away from edges to exclude intentional overshoot rebound. The PR310 paint/anchor/CPU harness was adapted for GTK’s native capture and bubble controllers. A coast is considered finished only after four frames with numerically identical adjustment values, not merely subpixel movement. The signal-only injection follows GTK 4.22’s controller ordering, including capture `decelerate` before bubble `scroll-end`; see [GTK’s implementation](https://github.com/GNOME/gtk/blob/gtk-4-22/gtk/gtkscrolledwindow.c). With no real Gdk event, controller units default to wheel units; deltas/velocities are normalized through GTK’s native scaling. This exercises smooth-sequence/native kinetics, but does **not** certify hardware touchpad event routing, cancellation, touchscreens, or boundary bounce behavior.

## Validation and remaining work

- `tests/test_render_vector.py`: all 159 pinned viewer vectors pass unchanged. `reading_turns`, `reading_markup`, `reading_word` and chip placement rules stay unchanged. D84 needs no data/vector update. Pixel baselines must be reviewed for removing ListView wrappers; this is not a claim of screenshot parity.
- Targeted transcript/meeting/render/refresh tests: 204 passed, 3 skipped on the host because PyGObject is absent there. Real GTK tests ran separately inside the QA desktop: PR310 exact measurements/cache checks, plus synchronous Meeting/Library/standalone transcript bindings, all passed. Ruff and `git diff --check` passed.
- Search: existing search-to-playhead routing can still use exact `scroll_to`. Search highlighting is not built here. Map original word IDs (including asides) to row/UTF-8 ranges; add non-metric Pango attributes to visible labels and scroll to the indexed row. A word-level vertical jump needs a Pango `index_to_pos` within that row. Do not derive offsets from escaped markup.
- Live recording: `update()` can replace the model and rebuild geometry, but full O(N) invalidation per update is unsuitable. Preserve unchanged per-width prefix entries and remeasure only the changed suffix, maintaining an anchor word plus local pixel offset. Rejoined/changed tail rows and speaker renames must invalidate every affected host/chip/header. This still needs real live-growth testing.
- Selection/copy: labels retain links but the prototype has no whole-transcript selection. Per-row `selectable` is straightforward; cross-row selection requires logical word/character endpoints independent of recycled widgets, clipboard prose from `row_text`, and visible-range painting. Recycling a selected widget must never discard the logical selection.
- Accessibility/focus: visible rows retain real name/time buttons and GtkLabel hyperlinks, avoiding custom glyph hit testing. Full document accessibility is **not** solved: virtual rows do not expose offscreen text, row collection metadata, complete keyboard traversal, or stable focus while recycling. Keep a focused row alive, implement materialize-before-focus navigation and indexed accessible row metadata, and validate Orca/AT-SPI. A virtual accessible text/document model may be needed. Reparented child ordering also needs an explicit accessibility audit.
- Tests to migrate if adopted: `tests/ui/listening-rows.py`, `meeting.py`, `wide-speakers.py`, `markup_runs_check.py`, `search.py`, `interjections.py`, `journey-parity.py` and helpers that inspect ListView factories/items, virtual GTK row wrappers or `page.store`. The synchronous binding fixture now includes the extracted row-builder helper. Keep D84 vectors intact and test renderer-independent word/row semantics.
- Packaging: two small Python modules added to Meson’s installed source list. No new runtime library, browser engine, network request, or package dependency. PangoCairo is part of the existing GTK stack. No package/container builds were run for this spike. GTK 4.18–4.20 compatibility still needs testing; the signal-injection harness deliberately asserts GTK 4.22’s two native kinetic controllers.

Migration estimate: roughly 2–3 weeks to make this a production alternative, dominated by nonblocking/incremental layout, geometry invalidation coverage, keyboard/focus/a11y, selection and renderer-independent UI tests. The exact prefix-sum core is small; preserving the whole GTK interaction contract is the expensive part. Do not ship this synchronous spike. If retained as fallback, first time-slice the up-front index while disabling scrolling until its extent is exact (or retain the old complete index until atomic replacement), then measure cold fonts, narrow widths, high contrast, language/scale changes, live growth and repeated open/close.

## Reproduction

Compile `src/cassini-talk.gresource.xml` into `cassini-talk.gresource` in a private shared build directory and compile `data/io.codemyriad.CassiniTalk.gschema.xml` there. In an **own** QA desktop with this worktree mounted read-only at `/mnt/source` and that build directory at `/mnt/shared`, run:

```sh
GSK_RENDERER=cairo GSETTINGS_SCHEMA_DIR=/mnt/shared \
CASSINI_CUSTOM_BUILD=/mnt/shared CASSINI_CUSTOM_OUT=/mnt/shared/results.json \
PYTHONPATH=/mnt/source/src:/mnt/source/tests \
python3 /mnt/source/tests/ui/transcript-custom.py
```

The script generates the finished 60-minute and four-hour fixtures using the PR310 generator, preserves exact observation samples in its output, and runs no wall-clock sleep polling. Check `free -m` (>4000 MB available) and acquire `/mnt/ai/cassini/dev/spikes/.heavy.lock` before the four-hour run; keep combined sandbox/app RAM under 2.5 GB and stop that desktop after measuring. Follow the brief’s shared-lock ownership rules. A power blackout interrupted the first QA session; the final run was restarted in the private custom desktop under the new RAM/serialization rule.
