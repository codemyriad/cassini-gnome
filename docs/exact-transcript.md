# Exact transcript renderer, Library Meeting page

`CASSINI_TRANSCRIPT_RENDER=custom` selects a Gtk.Scrollable with exact row heights on the Library Meeting page. The default ListView and standalone window are unchanged. GTK handles touchpad and touchscreen gestures, friction and overshoot; no replacement scroll controller is installed.

A reusable GTK/Pango probe builds an append-only pixel prefix in 4 ms CPU slices (one turn can overrun a slice). The first viewport paints before indexing finishes. Previously published rows do not shift as the range extends below them. Uncached resize widths build a replacement in the background and swap it atomically around the current row; old wrapping remains visible meanwhile. Three complete/partial width indices and per-turn metric markup are cached. The centered column caps at 720 px including headers. Its expanding text label skips an unused unwrapped natural-width layout; GTK retains the styled minimum, and every real height-for-width measurement stays native.

The regular `Exact transcript renderer` workflow runs the native GTK UI test, fresh ListView memory baseline, and budgets: first transcript paint <100 ms UI CPU, load/scroll/resize paint frames <32 ms, highlight <2 ms, RSS <= ListView +20 MiB. The fixture generator uses synthetic English only: 12,803 words / 484 rows and 51,212 / 1,933, plus the hour without diarization. It checks finger/coast in both directions, native touch-only drag/swipe, paused/playing, and the visible anchor during incremental indexing. Measured row heights must agree with live widgets. This is native-handler signal injection; physical hardware routing is not certified by a headless test.

Keyboard navigation covers arrows, Page Up/Down, Home/End, keypad variants and chronological Tab traversal, including unrealized rows. Alt listening shortcuts propagate. A focused row stays realized. Unchanged row widgets and accessible actions survive running-recording suffix replacement; the old exact range remains active until the changed suffix is measured. Touchscreen follow pause observes GTK's existing native gestures without claiming their sequences. Playback updates only visible rows; a retained overscan widget refreshes its highlight before its first visible paint.

The existing Meeting GtkLabels are not selectable and the page has no inline search/highlight controls. Library search routes a timed seek into the page. Native label link clipboard actions remain available; whole-document selection is not added. Standalone migration and the default switch remain follow-ups after Silvio tests a package.

## Accessibility

Both renderers expose native GtkLabel Text and Hypertext. On tested GTK 4.22, native virtual hyperlink children have no AT-SPI Action; their hyperlink object points back to the label, whose first action copies text. The exact renderer additionally exposes named native link buttons for visible timed words. These nonvisual buttons cannot intercept pointer events or take keyboard focus, and their native click action invokes the existing word seek. Native Text paints first; supplemental Action objects are created in 2 ms CPU chunks. Word geometry is cached per row, width and style generation rather than querying Pango on every scroll allocation. Only our escaped canonical markup is parsed, without importing a general-purpose HTML parser on opening. The row exposes its position and total set size; a group wraps its ordinary header and text. An independent AT-SPI process reads the text and activates a word, with the GUI verifying the resulting seek. Offscreen turns remain virtualized. Headless accessibility coverage does not certify a particular screen reader's speech presentation.

## Measurements in progress

The core revision passed the local native GTK matrix before the accessible actions were added. The final interaction revision must repeat the matrix before either PR becomes ready. Values below are UI-thread CPU milliseconds and process RSS MiB from fresh, separate baseline/exact processes, in the private GNOME QA desktop.

| Fixture | ListView RSS | Exact core RSS | First viewport paint | Worst load frame | Worst resize frame |
| --- | ---: | ---: | ---: | ---: | ---: |
| 60 min diarized | 161.9 | 147.4 | 55.7 | 15.3 | 12.8 |
| 4 h diarized | 172.4 | 160.2 | 43.0 | 11.5 | 13.4 |
| 60 min without speakers | 179.1 | 164.6 | 24.3 | 8.7 | 10.6 |

All modes had zero reversed frames. The added interaction revision subsequently passed the full local Xvfb matrix (70.8 / 67.9 / 45.0 ms first viewport paint, exact RSS 221.3 / 243.3 / 249.6 MiB versus ListView 261.9 / 286.7 / 287.9 MiB). After caching accessible geometry and budgeting action creation, the 60-minute run improved to 57.7 ms first paint, 16.1 ms worst load frame, 13.5 ms resize and 8.5–9.6 ms touchpad scroll frames. Hosted pinned-font Xvfb validation is still pending on this optimization; no local number alone establishes readiness. Final exact-head results and package links are recorded in `RESULT-production.md` by the coordinator.

## Checked bug classes

Reviewed [cassini-ux/review/BUG-CLASSES.md](https://github.com/codemyriad/cassini-ux/blob/main/review/BUG-CLASSES.md):

- 1 (long inputs): one append per turn/width, counters prohibit remeasurement after cancellation/cache return; 4x-length indexing CPU scaling is bounded in the UI test.
- 2 (UI thread): no synchronous whole transcript widget/index rebuild; work yields once per frame. Actual paint-frame budgets are asserted. Cold page construction outside paint may exceed 32 ms, as the spike reported; no end-to-end deadline guarantee is claimed.
- 3 (cache keys): indices are scoped to the model/header generation, width, font/DPI/scale. Theme, font/DPI/scale, header/language and live-tail changes invalidate the affected geometry; the UI test exercises rename, theme and font changes.
- 4/5/6 (ordering, word identity, zero-duration): retain unchanged FollowRows and reading_word projections; all original timed word and aside URIs are resolved in the UI test, with D84/transcript unit tests unchanged.
- 7/8/9/13 (chip hits, chip-only speakers, search, localization): retain the production row builder/sidebar/catalog; interaction checks cover synthetic chip hits, chip-only speaker actions and no-inline-search parity.
- 10–12, 14–16: no preview, format/export, I/O, download or storage changes.
- 17: measurements use thread CPU/frame-clock gates, never wall-clock pass conditions.
- 18/19: both PRs stay draft until exact-head CI and budgets pass. The coordinator explicitly requested publishing the intermediate stacked interaction draft; it cannot become ready before the base is green and ready.

## Reproduce

Compile the resource into a private directory (`glib-compile-resources --sourcedir=src src/cassini-talk.gresource.xml --target=.../cassini-talk.gresource`) and compile the settings schema. Set `CASSINI_CUSTOM_BUILD`, `GSETTINGS_SCHEMA_DIR`, `CASSINI_CUSTOM_OUT` and run `build-aux/ux-screenshot.sh tests/ui/exact-transcript.py output.png`. Run once with `CASSINI_TRANSCRIPT_RENDER=listview` for the baseline, then without it for the exact renderer. JSON output includes CPU, frame-clock latency, RSS and raw anchor samples. On roy, obey the >4000 MB available rule, acquire the shared heavy lock, use one private QA desktop and stop it afterward; no local Docker builds.
