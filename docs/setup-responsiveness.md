# Companion clicks and playback speed

The setup freeze reported on the fa1a41e Debian package has no confirmed cause.
This change fixes independently confirmed UI defects and ships diagnostics for a
future occurrence. It does not claim to resolve a specific GPU, paging, GIL or
accessibility crash.

## Diagnostics

The application installs a 50 ms main-context heartbeat and a daemon watchdog.
Once the context has begun dispatching, a heartbeat older than 250 ms logs
`main loop stalled N ms`, the process's major-fault delta and one all-thread
Python traceback to stderr/journal. A recovered heartbeat logs recovery and the
major faults since the stall. Each distinct stall is dumped once. SIGUSR1 also
dumps all threads without waiting for the threshold. Shutdown removes the timer
and signal handler without joining a worker on GTK.

Setup logs click-to-mapped milliseconds, including a repeated presentation. An
unavailable companion is logged rather than silently ignored. No user toast is
added. Browser launch/registration/profile detection remains worker-only.

## Behavior

Every outer Add/Set up control opens the setup sheet. Only its own first-step
button launches a browser. That step shows the opening caption until its worker
returns. A previously connected companion remains added after disconnecting.
Unmounted, closed or differently parented sheets are discarded and cleaned up.

The Meeting page owns its companion card above the transcript scroller; it no
longer shares one widget between virtualized ListItems. Stable browser snapshots
and stable meeting/menu state do not recreate UI each poll. The app caches its
recording Gio.Settings instance.

Playback speed is a real menu with six rates (0.75–2×). The window action keeps
the rate across folder/received-player replacements for the app session.
Ctrl+] / Ctrl+[ step through those rates. GTK's composite MenuButton is exposed
as a group with a named internal toggle owning the actual AT-SPI action.

Snap Chromium registration uses ~/snap/chromium/common/chromium. Supported
Flatpak browser desktop files map to ~/.var/app/<app-id>/config/<browser>;
registration also covers a concurrently installed native edition. Desktop Exec
text is parsed, never shell-evaluated. Host inventory runs through the existing
Flatpak host bridge, without extra permissions.

## Private gates

`tests/ui/companion-click.py` runs only under `ux-screenshot.sh`, cairo, private
Xvfb/D-Bus, an AT-SPI registry/listener, fake Chrome with two profile folders,
and a real private broker. BrowserConnection, BrowserWatch and BrowserActivity
stay live. The long fixture has 3,600 words throughout every measurement.
Each of three entry points is exercised in waiting/failed/needsBrowser states;
a 10 ms heartbeat checks the next two seconds, with an unchanged 100 ms bound,
and mapping must finish within 500 ms. It also checks retry, worker-only launch,
unchanged renders, stale sheets and card ownership after scrolling. Blocking
browser entry points, sockets, processes, sleeping, joining and unfinished
Future.result calls are guarded against GTK-thread execution. GTK warnings,
criticals and callback exceptions fail the child-process wrapper.

The source tests deliberately block a real GLib callback for 400 ms and assert
one stall/dump/recovery; a separate test checks SIGUSR1. The Meeting UI test
checks action/menu state, invalid rates, a real player replacement, Ctrl+] and
four seconds of synchronized GStreamer playback at 2× (8 seconds ±1 second).
The AT-SPI gate finds and activates the named speed control.

A private cairo container cannot validate the user's real GPU/desktop. The
coordinator's GPU/profile/second-build QA and both external reviews remain
required before merging. After the diagnostic build reaches the user, request
one reproduction with the journal, crash inventory and Cassini process list
specified in the coordination fix brief.
