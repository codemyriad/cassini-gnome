# Cassini browser recorder

Chromium extension for Linux and macOS. Choose **Record WebRTC tracks** for
separate incoming/outgoing sources, or **Record tab audio** for the tab's
rendered mix. The extension uses the same browser code and
[v1 protocol](../docs/browser-recording-protocol.md) as
[Cassini for Mac](https://github.com/codemyriad/cassini-mac).

## Record from the GNOME app

The app opens **Meetings in your browser**. Join a WebRTC meeting in a browser
with the companion loaded; the meeting appears automatically. Choose **Record**
on its row, then **Stop and save**. You can select a meeting while another tab
is active. Finished recordings can be opened from the app.

**Record every browser meeting automatically** is off by default. Turning it on
records newly detected meetings locally, including while the app window is closed.
Each automatic recording posts a notification with **Stop**. Stopping a meeting
prevents automatic restart until it ends and a new meeting is detected. One meeting
can record at a time per browser profile. The app starts separate-track capture;
whole-tab capture still requires a click in the extension popup.

For this checkout's separate development browser, no manual extension installation
is needed:

```sh
build-aux/run-browser-test.sh
_install/bin/cassini-viewer
```

The helper prepares both native hosts and loads the extension into
`~/cassini-ext-test/profile`. Restart that test browser after extension updates.
It writes recordings under `~/cassini-ext-test/recordings`. Normal Chrome profiles
use the installation instructions below and save under `~/Music/Cassini`.

Detection currently follows connected WebRTC audio. Sites which hide audio behind
a mixed receiver remain mixed; purely non-WebRTC meetings may require whole-tab
capture from the popup or the app's explicit **Record this computer…** control.
Audio counts are not participant identities.

## Install on Linux

1. Install Cassini natively. The build installs `cassini-browser-host` and the
   extension under `share/cassini-viewer/browser-extension`. For development,
   the checkout's `browser-extension` directory also works with system Python 3.
2. In native Chrome/Chromium 116+, open `chrome://extensions`, enable Developer
   mode, choose **Load unpacked**, and select that directory. Copy its ID.
3. Register the local host for that extension:

   ```sh
   python3 browser-extension/install-host.py EXTENSION_ID --browser chromium
   # For Chrome: --browser google-chrome
   # For a custom installation: --host /absolute/path/to/cassini-browser-host
   ```

   For a local build (`make install`), load
   `_install/share/cassini-viewer/browser-extension` and register its host with:

   ```sh
   python3 _install/share/cassini-viewer/browser-extension/install-host.py EXTENSION_ID --browser google-chrome
   ```

   The installed installer uses the configured host path. The checkout's
   installer prefers a host on PATH, then its development launcher. Keep the
   installation or checkout in place while using the extension.
   Only the supplied extension ID can connect. Custom browser user-data
   directories need the generated manifest in their `NativeMessagingHosts` folder.
4. Reload the meeting tab so the early hooks can observe connection creation.
   Join the meeting, invoke the Cassini extension, and choose a recording mode.
   Ctrl+Shift+9 opens the extension controls if that shortcut is available.
5. **Stop and save** finalizes all sources and opens the session in Cassini.
   The installed host locates the viewer and its settings under the installation
   prefix, including local builds outside PATH. You can also open the displayed
   directory's `session.json` in Cassini using the Recording Session file filter.

Source WAVs and `session.json` are saved under `~/Music/Cassini/Recording-…`.
The GNOME session viewer plays individual sources and shows their start offsets.
No audio is uploaded. An interrupted tab/browser connection preserves audio
already received and marks the session as interrupted. The tab capture path
keeps normal meeting playback audible and continues when the popup closes.

To remove the registration, delete
`~/.config/chromium/NativeMessagingHosts/com.codemyriad.cassini.json`
(or the equivalent `google-chrome` path), then remove the extension.

## What is captured

- Early MAIN-world hooks discover WebRTC receivers/senders; DOM media streams
  provide a recovery path. Local sender mute, suspension and track replacement
  are respected. Removed local previews are excluded from the generic fallback.
- Each source has independent 16-bit PCM audio, sequence numbers, render-clock
  timestamps and a start offset on the session timeline. Replacements are new
  sources; track IDs and labels are **not verified participant identities**.
- Tab fallback records the rendered mix, usually excluding your microphone.
  Use native microphone + desktop recording when browser sources are unavailable.
- Frames are captured only for the selected tab during an explicit recording.
  The extension installs discovery hooks on HTTP(S) pages at document start.
  Page-supplied data cannot choose output paths or send native control commands.

The session format is raw recording material, distinct from Cassini portable
Opus meetings. It does not generate transcripts or automatically identify people.
Firefox/Safari, arbitrary Web Audio graph interception, application-specific
participant-name adapters, and sandboxed-browser/Flatpak native-host integration
are not implemented. Use a native browser and native host for this path; the
GNOME native recorder also remains available.

See [recording tests](../tests/recording/README.md) for reproducible validation.
