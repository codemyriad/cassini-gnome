# Recording page and recording with Cassini hidden

W4-1 implements boards 11/12 and 15/16 with the shared copy catalog. The live
Recording page opens from the Library row or timer pill and after native capture
starts. Title edits and each note line are stored in app-owned meeting.json;
notes retain the first-keystroke time while edited. No session/audio bytes are
rewritten. Text focus preserves spaces and arrow keys for editing.

Native microphone/system meters use actual GStreamer levels; browser meters
read at most one second of complete PCM per validated active track. Remote
voices are counted from live remote tracks. The microphone warning starts after
20 seconds below -55 dBFS; Check opens GNOME sound settings. A source becoming
silent between observations is covered by the recent one-second sound window.
A Browser-only window can supply controller paths even without a Library window;
models are scanned off GTK and refreshed when tracks join or end. Capture
liveness always comes from the controller, never file age.

GNOME's GlobalShortcuts portal owns the Ctrl+Alt+R registration and desktop
permission. The shortcut toggles capture; an accepted first-run preference lets
it start without presenting a hidden window. Session/request cleanup and denial
are handled. Native and browser capture inhibit suspend while active. Hidden
capture updates one GNotification each second with Stop and Open Cassini; later
updates use low priority. The browser notification uses the shared catalog body.
Until the coordinator publishes native-specific body copy (R16), native capture
shows its actual title rather than claiming it records a browser.

Disk space keeps max(1 GiB, 2% capacity) in reserve. Estimated minutes use active
PCM byte rates. Below 30 minutes there is a line and one warning, without a
recovery button; a below-10-minute start is allowed and immediately warned.
Reaching reserve stops and keeps the capture, and the affected saved Meeting
retains the stopped line. Browser stop is scoped by capture path, works even
when meeting inventory is absent, and requires the broker's stopped state before
starting writing down. This changes app/broker IPC only, not the extension or
shared browser protocol.

Run checks with the established private harnesses:

```sh
CASSINI_TEST_BUILD="$PWD/_build-shot" build-aux/ux-screenshot.sh tests/ui/recording.py /tmp/recording.png
tests/recording/run.sh /usr/bin/python3 tests/recording/comfort.py --build _build-shot
CASSINI_TEST_BUILD="$PWD/_build-shot" tests/recording/run.sh node tests/recording/app-control.mjs
build-aux/ux-boards.sh _install-shot/bin/cassini-viewer /tmp/ux-boards
```

The first proof exercises portal wire requests (including immediate responses,
denial, activation and cleanup), actual GNotification payloads/actions, title and
pinned-note persistence, silent/disk UI, and browser-only hidden capture with a
newly joined track. The Pulse proof uses real private sound, keyboard events,
portal activation and notification actions, one disk warning, allowed low-space
start, reserve stop, and the final keystroke surviving quit. It allows a fresh
window after hidden capture completion. The extension proof retains real
Chromium/app Record/Stop, automatic recording, notification Stop and saved WAVs.

All displays, session buses, HOME/XDG state and Pulse servers are private. The
harness never targets the user's Wayland desktop, browser profile, audio device
or sound-settings window. The board fixture derives four remote tracks with an
audible tail and a silent microphone, without changing shared fixture bytes or
expected outputs. The artifact contains light/dark 1120x720 recording.png and
notification.json. Native-only wording and actual Shell notification chrome are
explicit platform/catalog limits, not screenshots of fake Shell widgets.

Background completion retires only windows hidden by Keep recording. Reopening
Cassini removes the other hidden windows, while an active legacy recorder window
stays owned until its recording finishes. A background Stop persists any saved
meeting's writing-down intent before removing the final window; the normal queue
keeps its own application holds until it finishes. This path does not call the
explicit Quit cancellation path. Stop and save only begins that cancellation
after capture finalization and the resumable intent checkpoint succeed. A native
error followed by a finalized recording is saved, while a preparation/legacy
failure without a finished signal keeps the window visible for diagnosis.

The private-Xvfb process-exit probe covers native and simulated browser hidden
completion with a delayed writing-down job, Keep recording with two windows,
reopening two hidden windows, and interrupted native finalization. The browser
stop unit cases cover a concurrent completed stop and an already-running save.

Live note editing maps body edits independently of the displayed timestamp spans:
splitting a line timestamps the new line, typing before a timestamp edits that
note's body, and multi-line deletion/undo retains the intended bodies and times.
Pending title/note drafts belong to the application editor, so reopening before
a scanner debounce or receiving an old activity snapshot cannot replace an edit.
Failed saves retain their dirty fields for retry. Titles use the schema's
500-character limit in the entry and writer, preserving batched notes.

Missing desktop sound settings are caught as a launch diagnostic. Native capture
notifications also work when the Library window is absent. Recording-page Space
cannot play a previously selected meeting, Export follows pushed-page state,
and repeated browser Stop buttons are disabled while their save is pending.
The default native capture path was already canonicalized by #35; a regression
asserts this under a symlinked Library.
||||||| parent of d482279 (Retain the last browser observer behind a visible player)
When the only visible window is a legacy player, it cannot take over browser
polling from a hidden Library/Browser window. The hidden observer stays owned
until capture saves, then the same saved-intent checkpoint runs before that
window closes. A new capture started during the asynchronous checkpoint keeps
its observer/windows. The process probe covers a browser-only hidden Library
with a visible legacy player and verifies completed writing-down before exit.
