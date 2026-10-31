# Contributing

Thanks for helping. Issues and pull requests are welcome on
[GitHub](https://github.com/codemyriad/cassini-gnome).

## Reporting bugs

Include your distribution, how you installed Cassini (Flatpak, .deb, .rpm, Arch package, source)
and, if you can share it, a file that shows the problem. A file that will not
open is often explained by the format's reference reader:
`tools/cassini-extract.py file.opus` in
[cassini-format](https://github.com/codemyriad/cassini-format).

## Development

```sh
build-aux/install-deps.sh   # build and runtime dependencies, any common distribution
make run                    # build, install into _install/ and run
```

`make help` lists the other targets. They wrap Meson and the scripts in
`build-aux/`, which you can also call directly.

The `development` profile uses the application ID
`io.codemyriad.CassiniTalk.Devel`, so it can run next to an installed release.

Layout:

| Path | What |
|---|---|
| `src/cassini_viewer/portable.py` | the format reader and audio digest; no GTK |
| `src/cassini_viewer/transcript.py` | turns, interjections, search, export; no GTK |
| `src/cassini_viewer/summary.py` | the summary Markdown subset; no GTK |
| `src/cassini_viewer/player.py` | GStreamer playback |
| `src/cassini_viewer/window.py`, `src/ui/` | the window |
| `data/` | desktop file, AppStream metadata, GSettings schema, icons, man page |
| `build-aux/` | dependency installer, distribution test matrix, and the Flatpak, Debian, RPM and Arch package builds |
| `src/cassini_viewer/runtime.py` | the startup check for missing or old libraries; no GTK |
| `debian/` | Debian packaging |

## Checks

CI runs all of these on every pull request:

```sh
make lint                   # browser-protocol/verify.py, ruff check and ruff format --check
pytest
make test                   # meson test: also validates the desktop, AppStream and schema files
make check-distros          # build and test on six distributions, in containers
```

CI also builds the .deb, both RPMs, the Arch package and the Flatpak.

The GTK-free modules have unit tests; please add tests with changes there.
For UI changes, follow the [GNOME Human Interface Guidelines](https://developer.gnome.org/hig/)
and include a screenshot in the pull request.

## Translations

Strings are marked with gettext. To add a language, add its code to
`po/LINGUAS` and create `po/<code>.po` from the template:

```sh
meson compile -C _build cassini-viewer-pot
msginit -i po/cassini-viewer.pot -o po/<code>.po -l <code>
```

## Releases

1. Update the version in `meson.build`, and the same version in:
   - a `<release>` in `data/io.codemyriad.CassiniTalk.metainfo.xml.in`
   - an entry in `debian/changelog`
   - `Version:` and a `%changelog` entry in `build-aux/rpm/cassini-viewer.spec`
   - `pkgver` in `build-aux/arch/PKGBUILD`

   The deb, RPM and Arch build scripts refuse to build when their version differs
   from `meson.build`.
2. Tag `vX.Y.Z` and push the tag. The release workflow builds the Ubuntu 26.04
   and Debian testing .debs (`cassini-viewer_X.Y.Z~ubuntu26.04_amd64.deb`,
   `cassini-viewer_X.Y.Z~debian-testing_amd64.deb`), the Fedora and openSUSE
   x86_64 RPMs, the Arch package and the Flatpak bundle, and attaches them to
   a GitHub release.

## Build from source

Cassini is a GTK 4 and libadwaita application written in Python, with GStreamer
for playback and recording.

You need Meson, Python 3.11+, PyGObject, GTK 4.18+, libadwaita 1.8+ and
GStreamer with the base and good plugins. To install them with your
distribution's package manager (apt, dnf, zypper, pacman or apk):

```sh
build-aux/install-deps.sh            # --dry-run to only print the command
```

Then build, test and run from a local prefix:

```sh
make run                             # or: make run FILE=recording.opus
make test
```

`make` only wraps Meson; by hand that is:

```sh
meson setup _build --prefix="$PWD/_install" -Dprofile=development
meson install -C _build
meson test -C _build
GSETTINGS_SCHEMA_DIR=_install/share/glib-2.0/schemas _install/bin/cassini-viewer recording.opus
```

To install system-wide, use a separate build directory, so that root does not
take over the development one:

```sh
make build BUILDDIR=_build-system PREFIX=/usr/local PROFILE=default
sudo meson install -C _build-system
```

Build Meson with the system Python, not one from a virtualenv: the launcher
runs with the interpreter Meson found, and that interpreter needs PyGObject.
Meson warns when it cannot import `gi`.

A plain build cannot write meetings down: the offline engine is off by
default (`-Dwhistle=disabled`), and the app then hides **Write it down** and
logs why. To bundle it (Linux glibc x86_64 only), download the pinned files
once and enable it; the build itself never downloads:

```sh
build-aux/whistle-cache.sh           # or: make whistle-cache
meson setup _build -Dwhistle=enabled # reads .whistle-cache (-Dwhistle_cache=DIR)
```

The app's Python also needs NumPy (`install-deps.sh` installs it, except on
Alpine). `-Dwhistle=enabled` installs the pinned ONNX Runtime wheel for that
Python privately (see below); a run from the source tree uses the Python's
own onnxruntime, if it has one.

### Packages

All of these write to `dist/`. The deb, rpm and arch targets build the
package in a clean container with podman or docker (set `CONTAINER_ENGINE` to
choose), then install it in a fresh container and smoke-test it there with
`--network none` (see below):

| Command | Builds |
|---|---|
| `make deb` | `cassini-viewer_X.Y.Z~ubuntu26.04_amd64.deb`, in Ubuntu 26.04 (`build-aux/deb/build.sh debian:testing` for `~debian-testing`) |
| `make rpm` | `.x86_64.rpm` and `.src.rpm`, in Fedora |
| `make rpm-opensuse` | `.x86_64.rpm` and `.src.rpm`, in openSUSE Tumbleweed |
| `make arch` | `.pkg.tar.zst` (x86_64), in Arch Linux |
| `make flatpak` | `cassini-viewer.flatpak` (`make flatpak-devel` for the development app) |
| `make packages` | all of them |

#### Writing down offline, in every package

Every package writes meetings down offline from its first launch, so each one
bundles the pinned **Whistle** weights, the **Needle** engine (glibc x86_64)
and wrapper, and **Silero VAD**, under `share/cassini-viewer/whistle`, and
the pinned **ONNX Runtime** wheel for its Python, unpacked under
`<libdir>/cassini-viewer/python`, with their licences (Apache-2.0, MIT) and
`NOTICE.md`. `whistle/pins.json` pins every URL and sha256;
`tests/test_packaging_pins.py` checks that the spec, the PKGBUILD and both
Flatpak manifests use exactly those.

- **Download once, build offline.** `build-aux/whistle-cache.sh` downloads
  the four files and the ONNX Runtime wheels (cp311 to cp314, 24 MB each)
  into `.whistle-cache/`, checking each sha256, with retries;
  `make deb|rpm|arch|packages` run it first, and CI caches it per
  `pins.json`. Meson (`-Dwhistle=enabled`) then runs `whistle/fetch.py
  --offline`, which never downloads and stops on a missing file or a hash
  mismatch, builds the small CPU-count bridge, and `whistle/verify.py` checks
  the bundle. `fetch.py --onnxruntime cpXY --offline` unpacks the wheel for
  the build's Python (without its model tools and C API library) and
  `verify.py --onnxruntime` checks it against the wheel's RECORD. The RPM
  takes the files as `Source1`-`Source5` (Source5: the wheel for
  `%{python3_version}`), the PKGBUILD as `source=` with `sha256sums` (the
  cp314 wheel, matching its `python>=3.14 python<3.15`), the Flatpak as
  `file` sources (cp313, GNOME 50's Python).
- **x86_64 only.** The engine is a glibc x86_64 binary, so the .deb is
  `amd64`, the RPM `ExclusiveArch: x86_64`, the Arch package `arch=(x86_64)`.
  The Flatpak builds everywhere but bundles the engine and its Python wheels
  only on x86_64. Elsewhere, and on Alpine (musl cannot load the engine), the
  app hides **Write it down** and logs "Writing down is not available on this
  computer" with the reason.
- **Never strip the bundle.** The app checks every file against
  `manifest.json` before it loads anything, so a stripped library would never
  run: `dh_strip`/`dh_dwz -Xwhistle/ -Xcassini-viewer/python/`, no RPM
  debuginfo or brp-strip (and no `libneedle.so` or ONNX Runtime provides),
  `options=(!strip !debug)`, Flatpak `no-debuginfo`. Each build verifies the
  installed bundle and ONNX Runtime again after those steps.
- **Runtime dependencies.** NumPy for the app's Python: `python3-numpy`
  (Debian, Ubuntu), `python3dist(numpy)` (Fedora, openSUSE), `python-numpy`
  (Arch); the Flatpak runtime has none, so the manifests install the pinned
  cp313 NumPy 2.5.4 wheel. Never the distribution's ONNX Runtime: on Fedora
  it pulls in sympy and, with default weak dependencies, about 1 GiB; the
  private wheel needs only NumPy. Because that wheel is built for one
  CPython ABI, each package depends on exactly its Python version:
  `python3 (>= X.Y~), python3 (<< X.Y+1)` (a `cassini:Python` substvar from
  `debian/rules`), `python(abi) = %{python3_version}`, `python>=3.14
  python<3.15`. A new distribution Python needs a rebuild (and, for Arch,
  the PKGBUILD's wheel moved with its bound); a Python newer than cp314 needs
  its wheel pinned in `pins.json`. Move the Flatpak's wheels with
  `runtime-version`. Nothing is downloaded at run time, the Flatpak has no
  network permission, and telemetry is off.
- **The offline smoke test.** After installing the package in a fresh
  container, each build script saves that container as a local image
  (`cassini-pkgtest-installed:*`) and starts it with `--network none` and a
  read-only root. There `build-aux/smoke-installed.py --whistle tests/whistle`
  checks the bundle's hashes and the private ONNX Runtime (and that the
  app's Python has no onnxruntime of its own), then writes both D11 goldens
  down with the
  installed worker: segments and word texts exact, word times within 80 ms
  (D11.3). `build-aux/flatpak/build.sh --smoke` (and CI) does the same in the
  installed Flatpak with `--unshare=network`.

The Flatpak is built on the host. It needs `flatpak` and either `flatpak-builder`
or the `org.flatpak.Builder` app. The script adds the Flathub remote to your
user installation if needed; the first build downloads the GNOME 50 SDK, and
later builds update it. You can also build it with GNOME Builder. CI runs build
the development app (`cassini-viewer-devel.flatpak`, ID
`io.codemyriad.CassiniTalk.Devel`), which installs next to a release.

`make check-distros` builds and tests Cassini on Debian testing, Ubuntu 26.04,
Fedora, openSUSE Tumbleweed, Arch Linux and Alpine edge, each in a fresh
container, and prints a table of the results. Pass images to
`build-aux/check-distros.sh` to try others.

### Runtime and GStreamer checks

If Python, PyGObject, GTK, libadwaita or GStreamer is missing or too old,
Cassini says which one and exits, rather than crashing; run `cassini-viewer`
in a terminal to see the message. If a GStreamer plugin is missing, it starts
anyway, warns on the terminal, and names the packages to install where the
plugin is needed: Ogg and Opus for `.opus` files, WAV for browser recording
tracks, and PulseAudio (`pulsesrc`), `audiointerleave`, `opusenc` and `oggmux`
for **Record Meeting**.

### Browser companion and recording architecture

For the separate development browser, run `build-aux/run-browser-test.sh` and
`_install/bin/cassini-viewer`. The helper loads the extension and registers both
native hosts automatically. See [browser setup](browser-extension/README.md) for
normal profiles, capture limits and the whole-tab fallback.

The companion is set up per user: each person registers the native messaging
hosts for their own browser profile. Packages install the host programs but
never register them system-wide. The browser companion does not work with the
Flatpak yet: the browser's native messaging hosts run outside the sandbox and
cannot reach the app, and the app cannot read the recordings they save under
`~/Music/Cassini`.

Native desktop recording uses PulseAudio or PipeWire's PulseAudio compatibility
service and the default input/output devices (`@DEFAULT_SOURCE@` and
`@DEFAULT_MONITOR@`). With both sources enabled, microphone is the left channel
and desktop audio the right, captured on one timeline. Recording inhibits
automatic suspend.

The [Chromium extension](browser-extension/README.md) captures separate WebRTC
sources or the meeting tab's rendered audio through a local native messaging
bridge. Cassini opens its recording sessions (`session.json`) and plays their
individual PCM WAV tracks. The browser protocol and WAV/session format are
shared with Cassini for Mac. See the
[isolated recording tests](tests/recording/README.md) for validation.

## The format

The reader in [`src/cassini_viewer/portable.py`](src/cassini_viewer/portable.py)
follows the [specification](https://format.gocassini.com/) and has no GTK
dependency. It reports the specification's trust states (`ok`, `stale-audio`,
`unverified`, `invalid-cassini-metadata`, …), and it verifies the
`exact-opus-audio-v1` digest of the audio packets in a background thread.
It passes the format's
[conformance vectors](https://github.com/codemyriad/cassini-format/tree/main/spec/conformance),
vendored in [`tests/conformance`](tests/conformance).

## Code of Conduct

This project follows the [GNOME Code of Conduct](CODE_OF_CONDUCT.md).

## License

By contributing you agree that your contributions are licensed under the
GPL-3.0-or-later, like the rest of the project.
