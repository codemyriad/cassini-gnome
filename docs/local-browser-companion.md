# Local browser companion setup (D39, work in progress)

Cassini prepares its reviewed browser extension in the host data directory:
`$XDG_DATA_HOME/cassini/browser-companion`, normally
`~/.local/share/cassini/browser-companion`. The Flatpak uses the existing host
bridge and the host's data directory. It adds no filesystem or D-Bus permission
and requires no Python on the host. Linux `flock` provides the shared lock
(util-linux or BusyBox; no timeout extension is needed).

The public manifest key must resolve to
`pcafdaahfhjiolfpfnlhmljfgoabbaij`. Resources are bounded regular files; symlinks,
private-key files and ambiguous transport names are refused. A same or newer
installed version keeps its files, inodes and timestamps. A newer app bundle is
staged beside the destination and published under a bounded kernel file lock with
a manifest compare-and-swap. Failed staging/rename retains the previous copy.
The browser reads an update at its next start. No browser preference, policy or
extension-installation database is edited.

`-Dstore_listing_public=false` is the default. It selects **Add from Cassini**.
When the listing is public, building with `true` selects the existing store
entry point and leaves local setup as a secondary action. Copying the folder or
opening a browser never marks setup complete: Done requires the actual native
hello/status handshake. Both entry points remain hidden with an unavailable
Flatpak bridge and disabled without a supported browser.

## Remaining guide amendment

R43/R44 in coordination's REPLIES-GNOME.md request the missing failure lines and
an explicit **Copy Extensions page address** action. In Chromium 149, external
`chrome://extensions/` arguments are rejected and open a New Tab. App mode and a
trailing slash do not change that result. The source validates external launch
URLs in [url_util.cc](https://github.com/chromium/chromium/blob/main/chrome/browser/ui/startup/url_util.cc).
The current Open button must be replaced by the approved explicit copy/paste
step before this work can ship. The catalog is not locally amended.

The real-browser test currently models that proposed explicit address copy in
its test driver, then uses actual address-bar keys, Developer mode, Load unpacked
and the native folder picker's Open button. This temporary scaffolding is marked
R44 and must become an activation of the production guide button when published.
It never loads an extension through a command-line flag, a CDP install API or a
profile edit. A private synthetic bundle version 0.2.3 checks stable-folder
updates on app/browser restart; the shipping extension remains 0.2.2.

## Validation

The source tests cover pinned identity, newer-only/idempotent writes, unsafe
files, partial staging, rollback and crash recovery, concurrent versions, an
busy owner, crash-released kernel lock, a host-data path, and a minimal
host PATH without Python or diffutils. The lock file remains in place so
all contenders use the same inode; there is no PID-based stale-lock cleanup. The installed Flatpak bridge smoke also
checks the real host-data copy and unchanged inode/timestamp on another launch.

The GUI test uses the existing private display harness. The fresh-browser proof
uses `tests/recording/run.sh` with a private Xvfb, D-Bus, Pulse server, XDG roots
and browser profile. It needs ImageMagick, at-spi2-core and gir1.2-atspi-2.0 in
addition to the existing recording dependencies:

```sh
CASSINI_TEST_BUILD="$PWD/_build" tests/recording/run.sh node tests/recording/companion-install.mjs
```

The Recording integration gate runs this proof against its exact build product.
The accessibility probe waits for the native address field to receive focus and
accept its paste before Enter; it never substitutes debug-protocol navigation
for that first installation.

It retains the real guide screenshot, native picker/extension identity/version,
connection evidence and stderr, and fails on Gtk/Adw warnings or criticals. No
Chrome Web Store request is permitted. The user's desktop and browser profile
are never used.
