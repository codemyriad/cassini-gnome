# Cassini

Cassini is a GNOME desktop app for Linux that records your meetings, writes them down, and lets you read and search them on this computer.

![The Library with sample meetings](docs/screenshots/library.png)

*The Library with sample meetings. The small `auto` tag next to a name comes from the sample data: recognising saved voices is not available yet.*

## What you can do

- Record a meeting from your microphone and this computer's sound (the others) straight into your Library with one press of **Record**.
- Write meetings down automatically on this computer, offline, using the speech model included in the app.
- Read a meeting while you listen, with each word highlighted as it plays and any word clickable to play from that moment.
- Search every meeting for a phrase and filter results by speaker, date, or meetings you recorded yourself.
- Open Cassini `.opus` files from elsewhere on your computer to keep a copy in your Library as a **Received** meeting.
- Export any meeting to share or back up, either as a portable Cassini `.opus` audio file or as a Markdown text file.
- Record calls in Chrome or Chromium so each person in the call is kept apart in the transcript.

## Get started

### Install

Packages are on the [Releases page](https://github.com/codemyriad/cassini-gnome/releases). The latest release, [v0.2.0](https://github.com/codemyriad/cassini-gnome/releases/tag/v0.2.0), has `.deb`, Fedora `.rpm`, openSUSE Tumbleweed `.rpm`, Arch, and Flatpak packages, but it is the earlier viewer: the Library, Recording page, writing down, Search every meeting, Received meetings, and Export described here come with the next release. Until then, you can download test packages built from `main` on each successful [CI run](https://github.com/codemyriad/cassini-gnome/actions/workflows/ci.yml?query=branch%3Amain) (a GitHub account is needed to download them), or build from source ([CONTRIBUTING.md](CONTRIBUTING.md)).

Native packages need GTK 4.18 and libadwaita 1.8 or newer (Debian testing, Ubuntu 26.04, Fedora 43 and later, openSUSE Tumbleweed, and Arch Linux). On older distributions (Debian 13, Ubuntu 24.04, openSUSE Leap, and RHEL), use the Flatpak. The test packages built from `main` are for 64-bit Intel and AMD (`x86_64`) computers: `.deb` packages for Ubuntu 26.04 and Debian testing, Fedora and openSUSE RPMs, an Arch package, and the Flatpak.

- **Flatpak** (uses the GNOME 50 runtime from Flathub; browser recording is not available in the Flatpak):

  ```sh
  flatpak install --user cassini-viewer.flatpak
  ```

- **Debian and Ubuntu**:

  ```sh
  sudo apt install ./cassini-viewer_*.deb
  ```

- **Fedora**:

  ```sh
  sudo dnf install ./cassini-viewer-*.rpm
  ```

- **openSUSE Tumbleweed**:

  ```sh
  sudo zypper install ./cassini-viewer-*.rpm
  ```

- **Arch Linux**:

  ```sh
  sudo pacman -U cassini-viewer-*.pkg.tar.zst
  ```

### Your first meeting

![The Library before your first meeting](docs/screenshots/first-run.png)

*The Library before your first meeting.*

Press **Record** in the Library (or press `Ctrl+R` anywhere in Cassini). Cassini starts recording your microphone and this computer's sound (the others) together, straight into the Library, without asking where to save a file.

The first time you record, a **Before your first recording** card reminds you that you are responsible for telling the people you record and explains what will be recorded. Choose **Don't show again** to hide the card next time. Your desktop may also ask you to allow Cassini to use your microphone.

![The Recording page during a sample meeting](docs/screenshots/recording.png)

*The Recording page with a sample meeting. `Change what is recorded` is shown disabled and is not available yet.*

While recording, the Recording page shows a large timer, live sound levels for your microphone and the others, an editable **Title**, and a **Notes** box. Each line you type in **Notes** is pinned to the moment you start typing it. If your microphone is silent for 20 seconds, Cassini shows a warning with a **Check** button that opens your sound settings.

Recording keeps going if you return to the Library. If you close the window, Cassini asks whether to **Stop and keep it** or **Keep recording** in the background. While Cassini is hidden, a desktop notification shows the active recording with **Stop** and **Open Cassini** buttons. You can also press `Ctrl+Alt+R` to start or stop recording from anywhere on the desktop (GNOME asks you to allow this shortcut the first time). Your computer will not go to sleep while Cassini is recording. If disk space runs low, Cassini warns you; if it gets too low, Cassini stops recording and keeps everything recorded so far.

When the meeting ends, press **Stop** on the Recording page, in the red pill in the Library header, or in the desktop notification. The Library shows the meeting saving, and then begins writing it down.

### Read and search your meetings

![A sample meeting open for reading and playback](docs/screenshots/meeting.png)

*A meeting page with a sample meeting. Speaker labels such as `Named by you`, `Recognised from saved voices`, and `auto` come from sample data and are not features yet.*

When a recording stops, Cassini writes it down automatically on this computer, offline, using the included Whistle speech model. The Library shows progress and an estimate of the time left. If you close Cassini, it continues next time you open the app; if writing down fails, press **Try again**. Older recordings and Received meetings without a transcript have a **Write it down** button. Writing down is available on `x86_64` (64-bit Intel and AMD) computers in both the native packages and the Flatpak; on other computers, the **Write it down** button is hidden.

In a meeting recorded from your microphone and computer sound, words are attributed to **Me** and **The others**. In browser recordings, each person in the call is kept apart as **Speaker 1**, **Speaker 2**, and so on.

In the Library, meetings are grouped by day with a quote, the speakers, and a short transcript preview. You can start playback straight from the preview, or open the meeting to see each speaker's share of the conversation and read the full transcript. As the meeting plays, words are highlighted and the transcript scrolls along. Click any word to play from that moment, use the player bar to skip 10 seconds back or forward, or press `Space` to play and pause.

![Searching across sample meetings](docs/screenshots/search.png)

*Search every meeting with sample meetings. Filtering by speaker uses speaker names already stored in a meeting; naming speakers and `auto` voice recognition shown in the sample are not features yet.*

To find something across all of your meetings, press the magnifier in the Library or press `Ctrl+F` to open **Search every meeting**. Type a phrase to find every matching moment. You can narrow results by speaker (when a meeting has named speakers), by date (**Today**, **Past 7 days**, or **Past 30 days**), and with **Recorded by me**. Each result shows the words around the match: press **Play from** to listen right there, or **Open the meeting here** to jump to that point in the full transcript. Filters reset when you close Search. The search index is private, kept in Cassini's cache folder, and never written into your Library.

### Open meetings you receive

![A sample Received meeting in the Library](docs/screenshots/received.png)

*A sample meeting in the Library marked as `Received`.*

When someone sends you a Cassini `.opus` meeting file, you can open it by choosing **Open…** in the main menu (`Ctrl+O`), opening the file with Cassini from your file manager, or dragging and dropping it onto the Library window.

Cassini copies the file into your Library and marks it as **Received**. Your original file is left untouched, and the copy in your Library stays even if you move or delete the original. Opening the same file a second time opens the existing copy instead of creating a duplicate.

### Export a meeting

![The Export dialog on a sample meeting](docs/screenshots/export.png)

*The Export dialog over a sample meeting. The note about recognised speakers comes from sample data; recognising voices is not a feature yet.*

To share a meeting or back it up elsewhere, press **Export…** in the Library header, in the meeting header, or in the main menu (or press `Ctrl+Shift+E`). You can export in two formats:

- **Cassini file**: a `.opus` audio file with the transcript, speakers, and notes inside, which plays in any audio player.
- **Markdown**: the transcript as a plain text `.md` file with speakers, times, and your notes.

Cassini saves exported files to your **Documents** folder unless you choose another folder, and never overwrites an existing file. **Export** is the only way a meeting leaves this computer. Voiceprints are never exported.

### Record meetings in your browser

In Chrome and Chromium, Cassini can record calls directly from your browser so each person in the call is kept apart as **Speaker 1**, **Speaker 2**, and so on. Browser recording works with the native packages, not the Flatpak. Until the companion is set up, a meeting may suggest **Set up**, which opens **Browser meetings**.

The browser companion is set up per user, for your own browser profile, and never system-wide:

1. In Chrome or Chromium, open `chrome://extensions`, turn on **Developer mode**, choose **Load unpacked**, and select `/usr/share/cassini-viewer/browser-extension`. Copy the extension ID shown on the card.
2. Connect the companion for your user account:

   ```sh
   python3 /usr/share/cassini-viewer/browser-extension/install-host.py EXTENSION_ID --browser chromium
   ```

   For Google Chrome, pass `--browser google-chrome`.

Then open the main menu in the Library and choose **Browser meetings…**. When you join a call, press **Record** on the meeting's row, and press **Stop** when you are done. You can also turn on **Record every browser meeting automatically** (off by default); a desktop notification with a **Stop** button appears whenever an automatic recording starts, even if the Cassini window is closed.

For local builds, whole-tab audio capture, and troubleshooting, see the [browser companion guide](browser-extension/README.md).

## Your meetings stay on this computer

Cassini stores your Library in `Music/Cassini` inside your home folder (`~/Music/Cassini`), with one folder per meeting.

Everything happens on this computer: recording, writing meetings down offline with the included speech model, and searching across your Library. There is no account, no cloud service, and nothing is uploaded. **Export** is the only way a meeting leaves this computer, and only when you choose.

Exported and Received [portable meetings](https://format.gocassini.com/) are ordinary `.opus` audio files that carry their transcript, speakers, and notes inside the file itself, and play in any audio player.

## What's coming

- Naming speakers in a meeting, keeping those names for future meetings, and recognising saved voices.
- Telling apart several people who share one microphone or one computer sound source.
- Changing what is recorded before you start (for example, recording your microphone only).

## Help and feedback

If you run into a problem or want to suggest an improvement, open an issue on [GitHub Issues](https://github.com/codemyriad/cassini-gnome/issues). Please include your Linux distribution and how you installed Cassini.

## For developers

To build Cassini from source, run the tests, or build packages, see [CONTRIBUTING.md](CONTRIBUTING.md). Technical documentation and specifications are in [`docs/`](docs/).

## License

Cassini for GNOME is free software, licensed under the [GNU General Public License, version 3 or later](COPYING). The conformance vectors in `tests/conformance` are CC0-1.0.
