# Custom transcript rendering spike — round 2, do not merge

Verdict: **only as fallback**, pending full keyboard/selection/accessibility work. Round 2 replaces the synchronous full index with frame-budgeted exact indexing, caps the text/headers column, and reuses complete or partial indices at previously measured widths. Native GtkScrolledWindow input and kinetics remain in control.

## Before / after

Paint-frame CPU targets passed on all three fixtures; first transcript paint was under 100 ms CPU. The broader cold first-page construction interval is called out below rather than hidden in the paint-only figures.

| Meeting | First transcript paint CPU ms, before → after | Worst load paint-frame CPU ms, before → after | Worst uncached resize paint-frame CPU ms, before → after | Above-cap resize frame CPU ms, after |
|---|---:|---:|---:|---:|
| 60min-diarized | 397.9 → **75.2** | 368.7 → **21.3** | 349.1 → **14.2** | 10.3 |
| 4h-diarized | 768.2 → **42.6** | 741.1 → **11.7** | 714.1 → **12.4** | 7.2 |
| 60min-no-speakers | 163.0 → **24.1** | 153.8 → **9.1** | 153.8 → **10.9** | 5.9 |

| Meeting | Full-index cumulative UI CPU ms, before → after | Full-index frame-clock latency ms, after | First transcript paint frame-clock latency ms, after | Rows indexed at first paint / total | App RSS MiB, before → after |
|---|---:|---:|---:|---:|---:|
| 60min-diarized | 379.5 → 714.4 | 1882.6 | 149.4 | 6 / 484 | 146.9 → 146.6 |
| 4h-diarized | 755.8 → 998.9 | 3327.9 | 62.4 | 11 / 1,933 | 158.9 → 158.9 |
| 60min-no-speakers | 155.0 → 317.8 | 916.5 | 49.9 | 3 / 127 | 160.4 → 162.4 |

Chunking improves responsiveness, not total CPU throughput: extra paints and the narrower column increase cumulative work. Full four-hour indexing finishes after about 3.3 seconds of frame-clock time; the first viewport appears after 42.6 ms CPU / 62.4 ms frame-clock latency. The first hour page encountered a cold construction interval of **60.2 ms UI CPU between paints**, even though its largest actual load paint frame was 21.3 ms. Thus this is **not** a guarantee that every end-to-end UI interval is below 32 ms. First-paint frame-clock latency also remains above 100 ms for that cold case (149.4 ms); the requested under-100-ms CPU target is met.

| Meeting | Reverse frames paused / playing (before and after) | `upper` changes after full load paused / playing | Round 2 observed frames paused / playing | Highlight mean / max CPU ms | Worst scroll paint-frame CPU ms paused / playing |
|---|---:|---:|---:|---:|---:|
| 60min-diarized | 0 / 0 | 0 / 0 | 941 / 935 | 0.144 / 1.005 | 5.88 / 5.50 |
| 4h-diarized | 0 / 0 | 0 / 0 | 936 / 934 | 0.103 / 1.401 | 4.86 / 5.22 |
| 60min-no-speakers | 0 / 0 | 0 / 0 | 940 / 942 | 0.093 / 1.163 | 3.69 / 3.71 |

While initially loading, `upper` grew on 102 / 195 / 51 sampled frames respectively, with **zero painted-anchor changes**. After loading it stayed fixed in all six scroll runs. Above-cap allocated widths stayed 720 → 720 → 720 px with zero measurements. Uncached/cached resize widths were 720 → 504 → 720 px; returning to 720 measured no turns. Interrupted narrow builds reused 9 / 12 / 4 already measured turns; final counters equalled exactly two widths’ worth of turns, proving no cached turn/width was remeasured. Largest uncached resize UI CPU intervals between paints were 14.5 / 12.5 / 11.2 ms. The cooperative 4 ms build budget permits one-turn overruns: observed largest slices were 14.0 / 5.1 / 6.2 ms, still keeping load paint frames under 32 ms.

All three fixtures passed actual word activation, current-word markup and follow checks. Worst scroll frame-clock gaps (separate from UI CPU) reached 90.1 / 49.1 / 33.3 ms on the shared host; native scrolling and compositor scheduling are not certified smooth at every presentation deadline.

Round 1 data are retained in [render-custom-round1-measurements.json](render-custom-round1-measurements.json); the current summary is [render-custom-measurements.json](render-custom-measurements.json). Raw round 2 observations: `/mnt/ai/cassini/dev/spikes/custom-qa/shared/results-round2-verified.json` on roy.

## What changed

Set `CASSINI_TRANSCRIPT_RENDER=custom` for the Library Meeting page only. The standalone reader and the default ListView path retain their renderers. The same real speaker/name/timestamp buttons, GtkLabel word links, current-word markup, inline chips and speakers pane remain in use. `reading_turns`, `reading_markup` and word URI resolution retain their D84 rules.

`ExactTranscript` implements Gtk.Scrollable and allocates real rows from measured prefix sums. It now uses a 4 ms UI-thread CPU budget per build slice, yielding after the current turn (an indivisible turn can exceed that budget), once per frame after letting the page chrome paint. Initial loading publishes only the exactly measured top-down prefix. Rows already published never move when later rows extend `upper`; first paint needs only enough measured rows to cover the viewport. No height estimates are introduced. The scrollbar initially represents the loaded prefix, growing until the full index is complete. An arbitrarily fast jump beyond the measured prefix must wait for measurement; hardware overshoot at that temporary end still needs testing.

For a previously unseen resize width, the old exact index and row wrapping remain visible while the replacement builds in the same bounded slices. A full replacement is then installed atomically, preserving the old top row and its local pixel offset. Thus a new-width reflow is delayed while indexing proceeds, with old-width text temporarily clipped/centered; it is not instant continuous rewrapping. Full-index load latency is reported separately from worst frame CPU. Uncached resize completion is likewise delayed by background measurement; its frame cost does not imply instantaneous reflow.

Three width indices cache the exact height of each turn at that width, including partially built indices. Returning to a completed width requires no text measurement. Cancelling a resize and later returning to its partial width resumes the remaining suffix. A centered 720 px cap, including the existing 120 px name column, makes wider window resizes allocation-only. The cap is internal to the Scrollable so no GtkViewport wrapper is introduced. This intentionally changes wrapping versus round 1’s 788/868 px columns; D84 data rules remain unchanged.

A single reusable, nonvisible probe row still measures with the same GTK/CSS/Pango machinery as real rows. Geometry-only word markup is cached per turn to avoid rebuilding it for each new width. PR310’s bounded Pango measurement cache and min-width shortcut remain. A raw shared Pango layout was not substituted: GTK’s link attributes, CSS line-height, padding, header sizing and ellipsis would require another equivalence proof; bounded GTK probe work met the measured paint-frame CPU targets without changing measurement semantics. No scroll controller or kinetic setting is replaced.

Transcript/header/dark-theme/font/DPI/scale changes invalidate indices. Arbitrary CSS-provider metric changes and high-contrast notification coverage still need an invalidation audit. Speaker changes may invalidate more rows than necessary. Live model replacement currently restarts indexing and does not preserve the playhead’s visual anchor; incremental tail updates remain required before using this for recording.

## Measurement method

Same synthetic fixtures and private bwrap GNOME Shell/Mutter 50.1 desktop as round 1: GTK 4.22, Cairo software renderer, 1280×800 monitor. The 60-minute fixture has 12,803 words/484 reading rows; the four-hour fixture has 51,212/1,933. Removing diarization/roster from the hour fixture produces 127 paragraphs, retaining the existing generic name/time header presentation. No real recordings were opened or copied. Models and Opus generation precede page-open timing; all coexist, so app RSS includes fixture/model retention. These are individual runs on the shared host, not statistically controlled benchmarks.

First-paint and full-index CPU use `thread_time_ns` from `open_meeting`. Frame CPU uses frame-clock before/after-paint; round 2 additionally records all UI-thread CPU between successive paints, including initial page construction and any work outside the paint interval. First-paint and full-index frame-clock latency are reported separately. Per-slice CPU and pure measurement CPU are instrumented inside the builder. No wall-clock sleep polling is used.

Every width’s visible row natural heights are checked against indexed heights in the scroll samples. Initial-load samples verify the top visible anchor stays fixed while `upper` grows. Real window resizes test: +80 px above the cap (zero measured turns); an uncached narrow width, an interrupted rebuild/return/resume, and return to the cached cap (zero repeated turn/width measurements). The total measurements must equal exactly two widths’ worth of turns.

After full loading, each fixture gets six flicks in each mode: three down and three up, finger deltas followed by GTK’s own kinetic ticker, paused and playing with follow suppressed. Actual visible row bounds define the anchor (1 px tolerance within the same row, zero row-index-reversal tolerance). Coast completion requires four numerically stationary frames. This adapts PR310’s paint/anchor/CPU harness to GTK 4.22’s native capture/bubble controllers, following [GTK’s ordering](https://github.com/GNOME/gtk/blob/gtk-4-22/gtk/gtkscrolledwindow.c). Signal-only injection defaults to wheel units and uses native scaling for smooth sequences; hardware touchpad routing, boundary bounce, touchscreens and cancellation are not certified. Navigation during an active native coast still needs a cancellation policy; normal hand-scroll follow cooldown avoids that conflict.

## Validation and remaining work

- `tests/test_render_vector.py`: all 159 pinned viewer vectors pass unchanged. `reading_turns`, `reading_markup`, `reading_word` and chip placement rules stay unchanged. D84 needs no data/vector update. Pixel baselines must be reviewed for removing ListView wrappers; this is not a claim of screenshot parity.
- Targeted transcript/meeting/render/refresh tests: 204 passed, 3 skipped on the host because PyGObject is absent there. Real GTK tests ran separately inside the QA desktop: PR310 exact measurements/cache checks, plus synchronous Meeting/Library/standalone transcript bindings, all passed. Ruff and `git diff --check` passed.
- Search: existing search-to-playhead routing can still use exact `scroll_to`. Search highlighting is not built here. Map original word IDs (including asides) to row/UTF-8 ranges; add non-metric Pango attributes to visible labels and scroll to the indexed row. A word-level vertical jump needs a Pango `index_to_pos` within that row. Do not derive offsets from escaped markup.
- Live recording: `update()` can replace the model and rebuild geometry, but full O(N) invalidation per update is unsuitable. Preserve unchanged per-width prefix entries and remeasure only the changed suffix, maintaining an anchor word plus local pixel offset. Rejoined/changed tail rows and speaker renames must invalidate every affected host/chip/header. This still needs real live-growth testing.
- Selection/copy: labels retain links but the prototype has no whole-transcript selection. Per-row `selectable` is straightforward; cross-row selection requires logical word/character endpoints independent of recycled widgets, clipboard prose from `row_text`, and visible-range painting. Recycling a selected widget must never discard the logical selection.
- Accessibility/focus: visible rows retain real name/time buttons and GtkLabel hyperlinks, avoiding custom glyph hit testing. Full document accessibility is **not** solved: virtual rows do not expose offscreen text, row collection metadata, complete keyboard traversal, or stable focus while recycling. Keep a focused row alive, implement materialize-before-focus navigation and indexed accessible row metadata, and validate Orca/AT-SPI. A virtual accessible text/document model may be needed. Reparented child ordering also needs an explicit accessibility audit.
- Tests to migrate if adopted: `tests/ui/listening-rows.py`, `meeting.py`, `wide-speakers.py`, `markup_runs_check.py`, `search.py`, `interjections.py`, `journey-parity.py` and helpers that inspect ListView factories/items, virtual GTK row wrappers or `page.store`. The synchronous binding fixture now includes the extracted row-builder helper. Keep D84 vectors intact and test renderer-independent word/row semantics.
- Packaging: two small Python modules added to Meson’s installed source list. No new runtime library, browser engine, network request, or package dependency. PangoCairo is part of the existing GTK stack. No package/container builds were run for this spike. GTK 4.18–4.20 compatibility still needs testing; the signal-injection harness deliberately asserts GTK 4.22’s two native kinetic controllers.

Migration estimate: roughly 1–3 weeks to make this a production alternative, dominated by incremental live-growth layout, geometry invalidation coverage, keyboard/focus/a11y, selection and renderer-independent UI tests. The exact prefix-sum core is small; preserving the whole GTK interaction contract is the expensive part. Do not ship this spike without the remaining interaction work. The frame-budgeted initial index and atomic resize replacement are now implemented; next measure cold fonts, narrow widths, high contrast, language/scale changes, live growth and repeated open/close.

## Reproduction

Compile `src/cassini-talk.gresource.xml` into `cassini-talk.gresource` in a private shared build directory and compile `data/io.codemyriad.CassiniTalk.gschema.xml` there. In an **own** QA desktop with this worktree mounted read-only at `/mnt/source` and that build directory at `/mnt/shared`, run:

```sh
GSK_RENDERER=cairo GSETTINGS_SCHEMA_DIR=/mnt/shared \
CASSINI_CUSTOM_BUILD=/mnt/shared CASSINI_CUSTOM_OUT=/mnt/shared/results-round2-verified.json \
PYTHONPATH=/mnt/source/src:/mnt/source/tests \
python3 /mnt/source/tests/ui/transcript-custom.py
```

The script generates the finished 60-minute and four-hour fixtures using the PR310 generator, preserves exact observation samples in its output, and runs no wall-clock sleep polling. Check `free -m` (>4000 MB available) and acquire `/mnt/ai/cassini/dev/spikes/.heavy.lock` before the four-hour run; keep combined sandbox/app RAM under 2.5 GB and stop that desktop after measuring. Follow the brief’s shared-lock ownership rules. The private custom QA desktop runs only during measurements; acquire the heavy lock before starting and release it after stopping that desktop.
