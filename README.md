# Cassini

Cassini is a GNOME desktop app for Linux that records your meetings and lets you read them back on this computer.

![The Library with sample meetings](docs/screenshots/library.png)

*The Library with sample meetings. Some sample rows show planned states—such as writing a meeting down on this computer (`Transcribing…`), recognising saved voices (`auto`), and `Needs attention`—that are not finished yet.*

## What you can do

- Browse your meetings in the Library, grouped by day, with a preview of the speakers and transcript and one-click playback for portable `.opus` meetings.
- Record a meeting on this computer from your microphone, the others on your computer's audio output, or both together.
- Record meetings in Chrome or Chromium with the browser companion, either when you press Record or automatically when you join a call.
- Open portable `.opus` meetings to listen while the transcript highlights each spoken word and scrolls along with playback.
- Click any word or timestamp to play from that moment, search within the transcript, and switch between transcripts when a meeting carries more than one.
- Read the meeting summary when a file includes one, and click its topics or action items to jump to that point in the audio.
- Check that a portable meeting's audio still matches its transcript, and export the transcript from the meeting window as plain text or WebVTT subtitles.

## Get started

### Install

Download a package from the [latest release](https://github.com/codemyriad/cassini-gnome/releases) (current version: 0.2.0), or grab a test build from the latest [CI run](https://github.com/codemyriad/cassini-gnome/actions).

Native packages need GTK 4.18 and libadwaita 1.8 or newer (available in Debian testing, Ubuntu 26.04, Fedora 43 and later, openSUSE Tumbleweed, Arch Linux, and Alpine edge). On older distributions (such as Debian 13, Ubuntu 24.04, openSUSE Leap, or RHEL), install the Flatpak.

- **Flatpak** (any distribution, using the GNOME 50 runtime from Flathub):

  ```sh
  flatpak install --user cassini-viewer.flatpak
  ```

  The Flatpak does not support the browser companion yet, because the browser cannot reach Cassini inside the Flatpak and the Flatpak cannot read browser recordings in `Music/Cassini`. Use a native package below if you want to record meetings from your browser.

- **Debian and Ubuntu**:

  ```sh
  sudo apt install ./cassini-viewer_*_all.deb
  ```

- **Fedora**:

  ```sh
  sudo dnf install ./cassini-viewer-*.fc*.noarch.rpm
  ```

- **openSUSE Tumbleweed**:

  ```sh
  sudo zypper install ./cassini-viewer-*.opensuse_tumbleweed.noarch.rpm
  ```

- **Arch Linux**:

  ```sh
  sudo pacman -U cassini-viewer-*-any.pkg.tar.zst
  ```

### Your first meeting

![The Library before your first meeting](docs/screenshots/first-run.png)

*The Library when you first open Cassini. Automatic writing down of new recordings shown in the hint is planned.*

Open Cassini to see the Library. Press **Record** (or press `Ctrl+R` from anywhere in Cassini) to open the recording window.

Choose your microphone, the others (desktop audio), or both, then press **Record…** and pick where to save the `.opus` file. Wear headphones when recording your microphone without a headset so your microphone does not pick up the others from your speakers.

When the meeting ends, press **Stop** (**Stop and Save** in the recording window, or close that window). Cassini finishes the file and opens your meeting for playback.

### Record meetings in your browser

With the browser companion for Chrome and Chromium, Cassini can detect meetings in your browser and record each person or audio source separately.

The companion is set up per user for your own browser profile, never system-wide. Installing a native Cassini package puts the companion files on your computer, and you connect them to your own browser profile:

1. In Chrome or Chromium, open `chrome://extensions`, turn on **Developer mode**, choose **Load unpacked**, and select `/usr/share/cassini-viewer/browser-extension`. Copy the extension ID shown on the card.
2. Connect the companion for your user account:

   ```sh
   python3 /usr/share/cassini-viewer/browser-extension/install-host.py EXTENSION_ID --browser chromium
   ```

   For Google Chrome, pass `--browser google-chrome`.

Open the menu in the top right of the Library and choose **Browser meetings…**. Join a call in your browser, press **Record** on its row, and press **Stop** (**Stop and save**) when you are done.

You can also turn on **Record every browser meeting automatically** (off by default). Cassini shows a desktop notification with a **Stop** button whenever an automatic recording starts, even when the Cassini window is closed.

For local builds, whole-tab audio capture, and troubleshooting, see the [browser companion guide](browser-extension/README.md).

## Your meetings stay on this computer

Cassini keeps your Library in `Music/Cassini` inside your home folder (`~/Music/Cassini`). Browser recordings are saved there automatically, and the Library watches that folder for changes.

Everything stays on this computer. There is no account, no cloud service, and nothing is uploaded.

Cassini also opens [Cassini portable meetings](https://format.gocassini.com/) from anywhere on your computer. A portable meeting is an ordinary `.opus` audio file that carries its transcript, speakers, and summary inside the file itself, so any audio player can play it.

## What's coming

- Writing meetings down on this computer with a bundled speech model, so new recordings get a transcript without leaving your machine.
- Telling speakers apart on this computer and recognising saved voices.
- Naming speakers in a meeting and keeping those names for future meetings.
- Searching across every meeting in the Library at once.
- Exporting transcripts and audio directly from the Library, and playing browser recordings directly from the Library preview.

## Help and feedback

If you run into a problem or want to suggest an improvement, open an issue on [GitHub Issues](https://github.com/codemyriad/cassini-gnome/issues). Include your Linux distribution and how you installed Cassini.

## For developers

To build Cassini from source, run the tests, or build packages, see [CONTRIBUTING.md](CONTRIBUTING.md). Technical notes and protocol specifications are in [`docs/`](docs/) and [`browser-protocol/`](browser-protocol/).

## License

Cassini for GNOME is free software, licensed under the [GNU General Public License, version 3 or later](COPYING). The conformance vectors in `tests/conformance` are CC0-1.0.
