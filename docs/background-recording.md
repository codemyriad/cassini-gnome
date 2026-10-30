# Background recording (D35.3)

The capture close dialog has Cancel, Keep Recording in Background, and Stop and keep it. Its Escape/close response is Cancel: it dismisses the dialog without hiding a window, requesting permission, acquiring a background hold or changing capture.

Only the explicit background choice calls the Background portal's `RequestBackground`, with `autostart: false` and no launch command. Permission response subscription precedes the method call. Until approval, the recording stays visible. Denial, unavailable service, a failed request or a bounded request timeout leaves it visible. A late approval cannot hide a finished recording. App holds cover the pending request and approved capture independently, and are released on every completion path.

A notification is sent immediately when hidden, even before the first Library scan. It remains available with Stop and Show actions; known meetings include their title and start time using the existing shared Recording-since copy. The notification is withdrawn on return or completion. The Background v2 status shows recording progress; status calls are deduplicated, flattened to one line and bounded to 96 characters. A desktop without SetStatus still supports the permission and notification paths. No automatic start or global shortcut permission is requested here.

`tests/ui/background.py` drives the real close dialog, native Escape, private portal responses and notification actions over D-Bus, with a real synthetic GStreamer capture. It covers Cancel, denial, pending approval, late approval after Stop, notification Show/Stop, errors, request cancellation and status bounds. The test rejects Gtk-WARNING/CRITICAL and saves a 1120×720 `background.png` in `ux-boards`. The 20 existing process-exit/hold scenarios in `tests/ui/quit.py` continue to run separately. Everything uses a private display, bus and synthetic audio.

## Windowless automatic recording (D41)

When automatic recording keeps Cassini alive after its last window closes, the app requests Background permission once and publishes “Ready to record browser meetings”. A live recording takes precedence. Returning to the Library clears the hidden status. The first successful idle status sends the catalog's one-time “Cassini is still running” notification; the preference survives restarts. Permission denial restores a window. No autostart command, tray icon, extra Flatpak permission or dummy microphone is added.

Background Apps' close button activates `app.quit`. With no visible window, the existing asynchronous stop-and-save guard finalizes every capture before quitting, without presenting an invisible confirmation. Launching the desktop entry reactivates the running application's Library.

Native builds register their actual installed desktop ID (`com.gocassini.Viewer` or `.Devel`) on a dedicated D-Bus connection before its first portal request. Connection/address discovery and registration run in a worker; calls are bounded. This does **not** make native apps appear in Background Apps on current GNOME. Ubuntu 26.04's xdg-desktop-portal 1.21.1 rejects their SetStatus with “Only sandboxed applications can set background status”, and its monitor enumerates Flatpak instances only. A pristine GNOME 50.1 probe confirmed successful Registry.Register/RequestBackground, that rejection, and an empty BackgroundApps array. The app does not send a notification claiming an unavailable entry can open it. This platform limitation is reported in coordination R48 and blocks claiming full D41 native support; there is no substitute tray implementation in this PR.

`tests/desktop/run.sh DEB FLATPAK OUTPUT` installs the real packages and runs a fresh headless GNOME 50 compositor, real portals, app registry, native application launcher and Background Apps UI. The desktop, session/system buses, audio devices and home are private to the Ubuntu 26.04 container. Flatpak uses nested unprivileged user/mount namespaces; the container drops all capabilities, permits the namespace syscalls, mounts no host device/display/bus/home and runs without network. Its cached runtime is acquired before starting the offline desktop. The JSON distinguishes native host exclusion from a passed Flatpak entry/open/status/close journey and verifies finalized WAV frame counts after quit. No native listing is fabricated. The installed-package desktop workflow consumes the same CI deb/Flatpak artifacts as the package checks. Fast private-bus GTK tests also cover permission caching, registry ordering, notification lifetime and hidden quit.

Registry API: <https://flatpak.github.io/xdg-desktop-portal/docs/doc-org.freedesktop.host.portal.Registry.html>.
Portal host rejection: <https://github.com/flatpak/xdg-desktop-portal/blob/8c3efadd0f3d619847c97965865c7a22c844a3c9/desktop-portal/background.c>.

## Optional indicator (D41.2 work in progress)

`status_notifier.py` implements the StatusNotifierItem and dbusmenu interfaces
through Gio. It watches an existing watcher without activating one, exports on
its unique session-bus connection, and registers only when a real host is
reported. Idle visibility and recording visibility are separate. A timer-label
change updates only the status row; an unchanged snapshot emits nothing.

The protocol references are [KDE's item interface](https://github.com/KDE/kstatusnotifieritem/blob/master/src/org.kde.StatusNotifierItem.xml)
and Ubuntu AppIndicator's [menu](https://github.com/ubuntu/gnome-shell-extension-appindicator/blob/master/interfaces-xml/DBusMenu.xml)
and [watcher](https://github.com/ubuntu/gnome-shell-extension-appindicator/blob/master/interfaces-xml/StatusNotifierWatcher.xml)
interfaces. `tests/ui/status-notifier.py` exercises the actual wire replies and
a fake watcher on a private session bus, including missing watcher/host, unique
registration, action guards, timer updates and teardown. Desktop integration,
Flatpak access, icon rendering and the GNOME privacy-indicator measurements
remain part of the D41 draft; the component is now wired into application and capture lifecycles.

D41.3 accepts that native builds do not appear in Background Apps on GNOME 50.
They use the optional indicator and app-grid/search activation. The portal
registry receives `get_application_id()`; this change does not rename desktop
IDs. The one-time notice names Background Apps only for a confirmed Flatpak
status, the indicator only after its host accepts registration, and otherwise
instructs the person to start Cassini again. Permission, registration and window
reopen races must settle before that notice is sent.
