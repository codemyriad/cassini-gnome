# Transcript rendering spike: TextView (do not merge)

Verdict: **only as fallback**. A single GtkTextView is a viable native-scrolling reader, but it is not an exact-layout architecture. GTK still validates paragraphs incrementally and changes the scroll range during opening and reflow. The completed synthetic tests show no reversed visible text anchors after validation. That evidence does not establish that scrolling can never jump during loading, reflow, or live edits. The effort to reproduce the current header, listening and accessibility interactions is substantial. These measurements argue against rejecting TextView outright on scrolling grounds, while retaining the independent assessment’s header/accessibility concerns.

Enable only on the Library Meeting page with `CASSINI_TRANSCRIPT_RENDER=textview`. The standalone reader and the default ListView path are unchanged. No custom kinetic handler is installed and `Gtk.ScrolledWindow:kinetic-scrolling` remains enabled.

The prototype uses one TextBuffer, hanging paragraph tags and a tab stop for the speaker/timestamp column. Inline replies are smaller tagged ranges with a chip background. Timed word ranges retain the repaired reading-model Word objects and Unicode character offsets. Every displayed host/reply word has a seek mapping; single clicks use TextView's native click gesture, preserving its selection handling. Enter at the caret seeks. A single current-word tag moves across temporal boundaries, including simultaneous words; ordinary playback does not scan or regenerate all transcript text. The existing Meeting-page follow suppression and reading-row policy is reused, with word-range visibility checks so the active word stays visible within a long paragraph. The speaker sidebar is unchanged.

## Measurements

| Case | Words / rows / chips | Reversals paused / frames | Reversals playing / frames | `upper` after layout |
|---|---:|---:|---:|---:|
| 60 min, diarized | 12,863 / 484 / 181 | 0 / 572 | 0 / 575 | 70,314, constant |
| 60 min, unattributed | 12,863 / 127 / 0 | 0 / 572 | 0 / 573 | 40,173, constant |
| 4 h, diarized | 51,484 / 1,935 / 725 | 0 / 572 | 0 / 570 | 283,446, constant |

Six flicks in each playback state for every case; zero frames with a changed `upper` during scrolling.

| Case | First painted rows (frame ms) | Fully validated (frame ms) | Synchronous open CPU ms | Worst load CPU ms (paint-only) |
|---|---:|---:|---:|---:|
| 60 min, diarized | 61.0 | 93.3 | 33.9 | 47.6 (11.3) |
| 60 min, unattributed | 30.4 | 76.1 | 17.4 | 30.2 (5.8) |
| 4 h, diarized | 130.9 | 279.9 | 101.2 | 115.8 (11.9) |

| Case | Highlight mean / max CPU ms | Worst scrolling CPU ms (paint-only) | Worst resize CPU ms (paint-only) | Process RSS MiB |
|---|---:|---:|---:|---:|
| 60 min, diarized | 0.011 / 0.082 | 2.45 (1.74) | 15.3 (5.2) | 121.5 |
| 60 min, unattributed | 0.012 / 0.051 | 3.38 (2.52) | 14.3 (4.6) | 132.9 |
| 4 h, diarized | 0.011 / 0.164 | 2.90 (1.65) | 17.0 (5.5) | 162.2 |

CPU maxima outside parentheses include all main-thread work between painted frames, including GTK validation idles; parentheses show only `before-paint` to `after-paint`. The highlight sample is 1,000 forward timing boundaries. During initial load, `upper` grows from 636 to 70,314, 2,643 to 40,173, 636 to 283,446 respectively; it is not constant until validation finishes.

[Synthetic prototype screenshot](textview.png).

## Method and limits

- Synthetic English only. Generator adapted from PR #310's `tests/transcript_scroll_support.py`: counts scaled to 0.78, with the same mixture of very short and long turns, overlapping speaker runs and folded replies; durations extended for four hours. The plain case uses the same finished one-hour audio and word text/timings, assigns every word to a single unattributed owner, removes the roster, and reprojects the reading rows. It has no speaker headers, interjection chips or overlap labels.
- Single runs on a shared host, not a controlled comparative benchmark. One private bwrap/headless GNOME desktop based on `/mnt/ai/cassini/qa/rootfs`, GTK 4.22.2, software Cairo rendering, 1280×800 monitor. App size 1120×700, transcript width 860 px; resize to 960×650, transcript width 700 px. Font 15 px, line height 1.5, 220 px hanging header margin. The resource bundle was copied from the existing QA `gnome-scroll-mixed-20261110` bundle; all Python was loaded from this worktree. No resource source files changed.
- Each fixture receives **six alternating down/up flicks per playback state**: five finger deltas then a 1.5-second coast measured by the frame clock. The harness emits GTK's native capture/bubble controller signals in their native order and asserts that the coast moves beyond finger release. Signal-only injection uses the native controller's wheel-unit scaling; it is not a physical touchpad/GdkEvent trace. Neither GTK's motion equations nor its adjustments are replaced by the test.
- Each painted-frame sample records the top visible text character offset and its intra-line displacement. A backwards character movement or more than one pixel of reverse motion within the same anchor counts as a reversal. Native kinetic overshoot is avoided by starting at 35% of the scroll range. Follow is explicitly suppressed throughout both scroll cases; playback uses a synchronized GStreamer fakesink.
- Open times use frame-clock timestamps. The synchronous open CPU is reported separately. Whole-buffer layout completion is observed with an idle watermark below `Gtk.TEXT_VIEW_PRIORITY_VALIDATE` (125 on this GTK), then another painted frame. This follows [GTK 4.22.2’s incremental validator](https://github.com/GNOME/gtk/blob/4.22.2/gtk/gtktextview.c#L5087), which runs at that priority until valid; it is a spike measurement, not a supported public “layout complete” signal.
- CPU has two useful scopes: `before-paint` to `after-paint` (render work), and main-thread CPU **between painted frames**, which includes TextView's validation idles and synchronous opening. The table uses the latter as the more conservative load/scroll/resize cost. It excludes blocked time and CPU on other threads. The [raw JSON](textview-measurements.json) includes both scopes and every scroll sample. RSS is whole-process current RSS after each case, with GTK/font caches retained within a process; the final four-hour case ran in a fresh process. It is not isolated renderer memory or peak RSS.
- Fixture generation and portable-file decoding are outside the measured page-open interval. These synchronous fixture operations trigger the application's stall watchdog in this harness; they are not page rendering measurements.
- Real GNOME virtual-pointer clicks, keyboard callback seeks, and active-word follow visibility are checked separately from the timed cases. Native momentum is canceled through its controller before re-enabling follow; otherwise the deliberately suppressed follow and still-coasting gesture would compete. The existing four-second manual-scroll suppression remains unchanged. Explicit seek during an ongoing coast still needs production interaction work.
- RAM rule: available RAM checked before desktop starts/heavy work; four-hour work serialized with the shared heavy lock. The app plus private shell remained well below 2.5 GB. The private desktop was stopped while waiting for the lock and after measurement.

## D84 and validation

`tests/test_render_vector.py`: **159 passed**. The reading model, turn grouping, timing repair, continuation rules, ownership and interjection ordering are unchanged. `tests/ui/transcript-textview-vectors.py` checks the six pinned viewer fixtures through the actual GtkTextBuffer: prose against `row_text`, all displayed seek owners, empty atoms and multibyte offsets. It passes. No D84 expected vectors need changing. The new backend must keep this projection check in addition to the model tests: preserving shared turns alone would not catch a widget dropping a folded reply.

This is semantic parity, not pixel parity: headers put name/time on one tabbed line, overlap captions prefix the body, chips have square text backgrounds, speaker-colour/provenance badges and future-word dimming are not reproduced. Long names can overrun the tab, and narrow windows need a stacked-header treatment. These are prototype limitations, not changes to D84 classification.

Targeted Ruff checks and Python compilation pass. No package build or entire test suite was run. Existing UI checks for the default ListView are not claimed to validate this flag.

## Migration and missing behavior

- **Search:** reuse the search index's original-word ownership, map each hit to its buffer range (including folded replies), apply a separate search tag, and jump with `scroll_to_iter`/a temporary mark. Keep selection, active-word and search tag priorities independent. Speaker labels must not become spoken search hits. Library search activation currently reaches this page through its timed seek; an in-buffer search UI is not built.
- **Live growth:** the prototype replaces the whole buffer when reading rows change. Production needs unchanged-prefix preservation, marks for selection/caret/follow targets, and replacement of only the changed tail. Rebuild timing indexes for that tail. Rejoined blocks can alter more than the last row. Full-buffer replacement today loses selection/caret and can move the viewport; the recording page is outside this flag.
- **Copy/selection:** native TextView selection and Ctrl+C are available; drag-selection suppresses click-to-seek. Copied text includes headings, timestamps, tabs and chip labels. A “copy transcript” action must project `row_text` separately if it needs clean export prose. Word double-click currently follows the native selection gesture, with the first click still seeking.
- **Accessibility:** TextView supplies native accessible text, caret and selection, which is a strong base. Tags do not expose each word as an AT-SPI hyperlink/action. Per-word seek actions, semantic speaker/time headers, keyboard discovery and screen-reader announcements require dedicated work and Orca testing. The sidebar retains its existing real accessible widgets, but the transcript's per-row listening highlight/name/time controls are not ported. Listening still operates from the unchanged sidebar.
- **Theme and layout:** replace hard-coded chip colours, add speaker-colour tags, future-word state, provenance badges and high-contrast tests. Production header widgets could use child anchors, but these complicate line baselines, wrapping, copy and accessibility; this spike deliberately measures plain text tags.
- **Tests:** `tests/ui/listening-rows.py`, `meeting.py`, `wide-speakers.py`, `transcript_bindings.py`, `markup_runs_check.py`, `search.py`, `interjections.py` and `journey-parity.py` contain row-widget/ListView assumptions. Parameterize semantic assertions by backend and add actual text-range/link accessibility checks. Keep the original backend tests until migration is complete. Standalone reader migration would additionally replace its per-row search/focus/scroll interfaces.
- **Packaging:** GtkTextView/TextBuffer/Pango are already dependencies. No WebKit, extra service, network requests or new deb/rpm/Arch runtime dependency. The new Python module is included in Meson's install list.

Estimated migration: **4–7 engineer-days** for a production Library page (responsive headers, theme/listening/search, incremental updates and interaction tests), plus **2–4 days** for the standalone reader and accessibility validation/remediation. This is an estimate, not measured delivery time. Accessibility remediation could extend it. Prefer evaluating the real-row approach first; retain TextView as a plausible simpler-document fallback, not as proof that GTK never estimates scroll geometry.

## Reproduce

In an isolated GNOME session with the usual GTK/Adwaita/GStreamer test dependencies and a built resource bundle:

```sh
PYTHONPATH=src:tests CASSINI_TRANSCRIPT_RENDER=textview \
 CASSINI_TEXTVIEW_RESOURCE="$BUILD/src/cassini-talk.gresource" \
 CASSINI_TEXTVIEW_OUTPUT=/tmp/textview-results.json GSK_RENDERER=cairo \
 python3 tests/ui/transcript-textview.py
python3 tests/ui/transcript-textview-vectors.py
```

The measurement harness requires GNOME Shell's unsafe Eval interface for real virtual-pointer input. `CASSINI_TEXTVIEW_HOURS=1` or `4` selects one duration; the default runs both and also the plain one-hour presentation. Acquire the shared heavy lock externally before the four-hour run and check `free -m`; the harness also rejects the four-hour case below 4000 MiB available. This is an explicitly invoked spike harness, not an automatically enabled production test.
