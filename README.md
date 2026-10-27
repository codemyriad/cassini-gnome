# Cassini for GNOME

Play meeting recordings and read along with the transcript stored inside them.

![Cassini following a meeting transcript](data/screenshots/transcript.png)

Cassini opens [Cassini portable meetings](https://format.gocassini.com/):
ordinary Ogg Opus files whose comment header also carries a word-timed
transcript, the speakers and, optionally, a summary. Any audio player can play
them. Cassini also shows the transcript.

- The spoken word is highlighted during playback, and the view follows it
- Click any word or timestamp to play from there
- See participants and who is speaking, and switch between transcripts when a file has several
- Search the transcript
- Read the summary and jump to its action items and topics
- Check that the audio still matches the transcript recorded with it
- Export the transcript as plain text or WebVTT subtitles
- Plain `.opus` files play without a transcript

It is a GTK 4 and libadwaita application written in Python, with GStreamer
for playback.

## Record from your browser

Cassini opens **Meetings in your browser**. With the companion connected, join a
WebRTC meeting and choose **Record** on its row. **Stop and save** finishes the
recording; **Open recording** plays it. The optional **Record every browser meeting
automatically** switch is off by default and posts a notification with **Stop**
for each automatic recording, including when the app window is closed.

For the separate development browser, run `build-aux/run-browser-test.sh` and
`_install/bin/cassini-viewer`. The helper loads the extension and registers both
native hosts automatically. See [browser setup](browser-extension/README.md) for
normal profiles, capture limits and the whole-tab fallback.

The companion is set up per user: each person registers the native messaging
hosts for their own browser profile. Packages install the host programs but
never register them system-wide.

Recording from the browser needs a native installation; the Flatpak does not
support the companion yet (see [Install](#install)).

## Record a meeting

Choose **Record Meeting…** from the app menu (Ctrl+R), select microphone,
desktop audio, or both, then choose an `.opus` file. **Stop and Save** finishes
the file and opens it in Cassini. Closing the recording window also saves it.
With both sources enabled, microphone is the left channel and desktop audio
the right, captured on one timeline. Recording prevents automatic suspend.

This uses PulseAudio or PipeWire's PulseAudio compatibility service and the
default input/output devices. Desktop capture includes notifications and other
applications. The microphone records independently of the meeting's mute
button; use headphones to avoid capturing the speakers twice. Recordings do
not yet include transcripts or speaker names.

The [Chromium extension](browser-extension/README.md) captures separate WebRTC
sources or the meeting tab's rendered audio through a local native messaging
bridge. Cassini opens its recording sessions and plays their individual sources.
The browser protocol and WAV/session format are shared with Cassini for Mac.
See the [isolated recording tests](tests/recording/README.md) for validation.

## Install

Cassini needs GTK 4.18 and libadwaita 1.8 or newer. These distributions have
them: Debian testing, Ubuntu 26.04, Fedora 43 and later, openSUSE Tumbleweed,
Arch Linux and Alpine edge. On older ones (Debian 13, Ubuntu 24.04, openSUSE
Leap, RHEL and its rebuilds) use the Flatpak, which bundles these libraries but
does not support recording from the browser yet.

Builds of every commit are attached to the
[CI runs](https://github.com/codemyriad/cassini-gnome/actions), and tagged
versions to the [releases](https://github.com/codemyriad/cassini-gnome/releases):

- **Flatpak**, any distribution: `flatpak install --user cassini-viewer.flatpak`.
  Uses the GNOME 50 runtime from Flathub. CI runs build the development app
  instead (`cassini-viewer-devel.flatpak`, ID `com.gocassini.Viewer.Devel`),
  which installs next to a release. The browser companion does not work with
  the Flatpak yet: the browser's native messaging hosts run outside the
  sandbox and cannot reach the app, and the app cannot read the recordings
  they save under `~/Music/Cassini`. Use a native package for that.
- **Debian and Ubuntu**: `sudo apt install ./cassini-viewer_*_all.deb`
- **Fedora**: `sudo dnf install ./cassini-viewer-*.fc*.noarch.rpm`
- **openSUSE Tumbleweed**: `sudo zypper install ./cassini-viewer-*.opensuse_tumbleweed.noarch.rpm`
- **Arch Linux**: `sudo pacman -U cassini-viewer-*-any.pkg.tar.zst`

If Python, PyGObject, GTK, libadwaita or GStreamer is missing or too old,
Cassini says which one and exits, rather than crashing; run `cassini-viewer`
in a terminal to see the message. If a GStreamer plugin is missing, it starts
anyway, warns on the terminal, and names the packages to install where the
plugin is needed: Ogg and Opus for `.opus` files, WAV for browser recording
tracks, and PulseAudio (`pulsesrc`), `audiointerleave`, `opusenc` and `oggmux`
for **Record Meeting**.

## Build from source

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

### Packages

All of these write to `dist/`. The deb, rpm and arch targets build the
package in a clean container with podman or docker (set `CONTAINER_ENGINE` to
choose), install it there and smoke-test it:

| Command | Builds |
|---|---|
| `make deb` | `.deb`, in Ubuntu 26.04 (`build-aux/deb/build.sh debian:testing` for Debian) |
| `make rpm` | `.rpm` and `.src.rpm`, in Fedora |
| `make rpm-opensuse` | `.rpm` and `.src.rpm`, in openSUSE Tumbleweed |
| `make arch` | `.pkg.tar.zst`, in Arch Linux |
| `make flatpak` | `cassini-viewer.flatpak` (`make flatpak-devel` for the development app) |
| `make packages` | all of them |

The Flatpak is built on the host. It needs `flatpak` and either `flatpak-builder`
or the `org.flatpak.Builder` app. The script adds the Flathub remote to your
user installation if needed; the first build downloads the GNOME 50 SDK, and
later builds update it. You can also build it with GNOME Builder.

`make check-distros` builds and tests Cassini on Debian testing, Ubuntu 26.04,
Fedora, openSUSE Tumbleweed, Arch Linux and Alpine edge, each in a fresh
container, and prints a table of the results. Pass images to
`build-aux/check-distros.sh` to try others.

## The format

The reader in [`src/cassini_viewer/portable.py`](src/cassini_viewer/portable.py)
follows the [specification](https://format.gocassini.com/) and has no GTK
dependency. It reports the specification's trust states (`ok`, `stale-audio`,
`unverified`, `invalid-cassini-metadata`, …), and it verifies the
`exact-opus-audio-v1` digest of the audio packets in a background thread.
It passes the format's
[conformance vectors](https://github.com/codemyriad/cassini-format/tree/main/spec/conformance),
vendored in [`tests/conformance`](tests/conformance).

## Contributing

Bug reports and pull requests are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md).
This project follows the [GNOME Code of Conduct](CODE_OF_CONDUCT.md).

## License

Cassini for GNOME is free software, licensed under the
[GNU General Public License, version 3 or later](COPYING).
The conformance vectors in `tests/conformance` are CC0-1.0.
