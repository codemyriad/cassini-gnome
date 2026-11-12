# W1-G3 screenshot spike

This hidden entry snapshots the real, unchanged current main window. It is not
an implementation of the Library boards. The existing browser-recording window
remains the default until UX1b/UX10 replace it.

Build and run in a separate worktree:

```sh
meson setup _build-shot --prefix="$PWD/_install-shot" -Dprofile=development
meson install -C _build-shot
build-aux/ux-screenshot.sh _install-shot/bin/cassini-talk ux-artifacts/current-main.png
build-aux/ux-shots.sh _install-shot/bin/cassini-talk _build-shot ux-artifacts
```

The wrapper gives the app a private HOME, XDG directories, D-Bus and Xvfb display;
Wayland is unset and both audio servers are unavailable. It enables the hidden
`CASSINI_SCREENSHOT_OUT` entry only for this environment. No desktop screenshot,
real browser, microphone or output device is used.

GTK WidgetPaintable → GtkSnapshot → Gsk render node → GdkTexture writes the PNG.
The harness waits for the initial model refresh and subsequent painted frames,
then waits for a nonempty render node. The image includes the window widgets and
header, but not compositor decorations/shadows. A requested 1120×720 window
currently allocates an 1110×710 widget image; the JSON records the actual size.

The CI workflow pins Ubuntu 26.04, Cantarell 0.303.1-4 and Inter 4.1+ds-1. A
private fontconfig setup selects only those fonts, with Cantarell 11 as the app
font. It forces Cairo, light or dark style (`CASSINI_SCREENSHOT_SCHEME`) without
pinning a system accent, with Europe/Rome and en_GB.UTF-8. `build-aux/ux-shots.sh`
renders every board and UI flow in light and dark and writes `shots.json`. PNG,
environment metadata, toolkit versions (`packages.txt`) and font versions/hashes are uploaded.
On pull requests, `build-aux/ci/pr-shots.py diff` compares `ux-artifacts` against the
`main` baseline `ux-boards` artifact, appends the link table to `$GITHUB_STEP_SUMMARY`,
and uploads `pr-shots/` (`changes.json`, `block.md`, and changed before/after/diff PNGs).
For local use without installing fonts system-wide, extract those font packages
and set `CASSINI_SCREENSHOT_FONT_DIR` to their `usr/share/fonts/opentype` folder.

What did not work initially: checking the requested size ignored GTK's CSS
margins, and capturing immediately after refresh could yield an empty or stale
paintable. Readiness now uses allocation and painted frames. Fixed fixture/clock
rendering, a board gallery, pixel comparison and the Library shell are outside
this spike; the coordinator's S-4 contract will define them.
