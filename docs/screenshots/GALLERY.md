# QA screenshot gallery (D-1157)

`build-aux/ux-gallery.sh BUILD_DIR OUT_DIR [SCREEN...]` captures the main
screens of the real app for design review. CI does not run it. Each screen runs
in its own private Xvfb display, D-Bus session and home (through
`build-aux/ux-screenshot.sh`), in light and dark, at two window sizes:

| Size | Default size | Window | What it shows |
|---|---|---|---|
| wide | 1130×730 | 1120×720 | the Library beside the meeting |
| narrow | 370×790 | 360×780 | phone width: the sidebar has collapsed |

First Run, Library and Meeting also run with Large Text (wide only). GNOME's
Large Text is `text-scaling-factor` 1.25, which GTK applies as Xft DPI 120, so
the gallery sets that DPI. Scale factor is 1.

The data comes from the shared Library fixtures (`ux/library-vectors/make.py`,
plus `tests/ui/recording-fixture.py` for the live recording). The fixture
library is moved to `~/Music/Cassini` in the private home, so paths on screen
look like a real user's. Each window is grabbed from the Xvfb root, so menus
(which have their own surfaces) appear in the shot. Xvfb has no compositor, so
there is no shadow and there are no rounded corners to trim.

Files are named `gnome-NN-screen[-narrow|-large-text]-light|dark.png`. Next to
them, `SCREEN-SIZE[-large-text]-SCHEME.json` records the GTK and libadwaita
versions, the scale factor, the Xft DPI and the size of each shot. A tall
preferences page also gets an `-end` shot, scrolled to the bottom.

Locally, in an Ubuntu 26.04 image with the packages the `ux-shot` workflow
installs (`.github/workflows/ux-shot.yml`), Xvfb and Pillow:

```sh
docker run --rm -v "$PWD":/src:ro -v "$PWD/gallery":/out IMAGE bash -c \
  'cp -r /src /build && cd /build && meson setup -Dprofile=development --prefix=/tmp/inst build \
   && meson install -C build && build-aux/ux-gallery.sh build /out'
```
