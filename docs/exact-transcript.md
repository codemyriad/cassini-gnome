# Exact transcript renderer, step 1a

The Library Meeting page uses the exact-height Gtk.Scrollable by default. For one release, launch with `CASSINI_TRANSCRIPT_RENDER=listview cassini-talk` to use the previous renderer. The standalone window will follow in a separate PR. GTK handles touchpad and touchscreen gestures, friction and overshoot; no replacement scroll controller is installed.

A reusable GTK/Pango probe builds an append-only pixel prefix in 4 ms CPU slices (one turn can overrun a slice). The first viewport paints before indexing finishes. Previously published rows do not shift as the range extends below them. Uncached resize widths build a replacement in the background and swap it atomically around the current row; old wrapping remains visible meanwhile. Three complete/partial width indices and per-turn metric markup are cached. The centered column caps at 720 px including headers. Its expanding text label skips an unused unwrapped natural-width layout; GTK retains the styled minimum, and every real height-for-width measurement stays native.

The regular `Exact transcript renderer` workflow runs the native GTK UI test, fresh ListView memory baseline, and budgets: first transcript paint <100 ms UI CPU, load/scroll/resize paint frames <32 ms, highlight <2 ms, RSS <= ListView +20 MiB. The fixture generator uses synthetic English only: 12,803 words / 484 rows and 51,212 / 1,933, plus the hour without diarization. It checks finger/coast in both directions, native touch-only drag/swipe, paused/playing, and the visible anchor during incremental indexing. Measured row heights must agree with live widgets. This is native-handler signal injection; physical hardware routing is not certified by a headless test.

Step 1b will add keyboard/focus/accessibility parity and incremental recording-tail updates before the feature is ready for Silvio's package trial. The current page has neither selectable transcript text nor inline search/highlight controls; its Library search routes a timed seek into the page. Both facts will be checked against the baseline. The standalone migration follows in its own PR.

## Checked bug classes

Reviewed [cassini-ux/review/BUG-CLASSES.md](https://github.com/codemyriad/cassini-ux/blob/main/review/BUG-CLASSES.md):

- 1 (long inputs): one append per turn/width, counters prohibit remeasurement after cancellation/cache return; 4x-length indexing CPU scaling is bounded in the UI test.
- 2 (UI thread): no synchronous whole transcript widget/index rebuild; work yields once per frame. Actual paint-frame budgets are asserted. Cold page construction outside paint may exceed 32 ms, as the spike reported; no end-to-end deadline guarantee is claimed.
- 3 (cache keys): indices are scoped to the model/header generation, width, font/DPI/scale. Theme/provenance/language notifications and live-tail invalidation are covered in step 1b before readiness.
- 4/5/6 (ordering, word identity, zero-duration): retain unchanged FollowRows and reading_word projections; all original timed word and aside URIs are resolved in the UI test, with D84/transcript unit tests unchanged.
- 7/8/9/13 (chip hits, chip-only speakers, search, localization): retain the production row builder/sidebar/catalog; complete interaction checks and synthetic chip punctuation hit coverage follow in 1b.
- 10–12, 14–16: no preview, format/export, I/O, download or storage changes.
- 17: measurements use thread CPU/frame-clock gates, never wall-clock pass conditions.
- 18/19: keep draft until exact-head CI is green; step 1b will not be opened on a draft/failing base.

## Reproduce

Compile the resource into a private directory (`glib-compile-resources --sourcedir=src src/cassini-talk.gresource.xml --target=.../cassini-talk.gresource`) and compile the settings schema. Set `CASSINI_CUSTOM_BUILD`, `GSETTINGS_SCHEMA_DIR`, `CASSINI_CUSTOM_OUT` and run `build-aux/ux-screenshot.sh tests/ui/exact-transcript.py output.png`. Run once with `CASSINI_TRANSCRIPT_RENDER=listview` for the baseline, then without it for the exact renderer. JSON output includes CPU, frame-clock latency, RSS and raw anchor samples. On roy, obey the >4000 MB available rule, acquire the shared heavy lock, use one private QA desktop and stop it afterward; no local Docker builds.
