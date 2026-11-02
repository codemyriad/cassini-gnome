# Local browser companion setup (D39/D39.1)

Cassini prepares its reviewed browser extension in the host data directory:
`$XDG_DATA_HOME/cassini/browser-companion`, normally
`~/.local/share/cassini/browser-companion`. The Flatpak uses the existing host
bridge and the host's data directory. It adds no filesystem or D-Bus permission
and installs no host helper or Python dependency. Linux `flock` provides the
shared lock (util-linux or BusyBox; no timeout extension is needed).

The public manifest key must resolve to
`pcafdaahfhjiolfpfnlhmljfgoabbaij`. Resources are bounded regular files; symlinks,
private-key files and ambiguous transport names are refused. A same or newer
installed version keeps its files, inodes and timestamps. A newer app bundle is
staged beside the destination and published under a bounded kernel file lock with
a manifest compare-and-swap. A refresh exchanges complete directories in one
`renameat2(RENAME_EXCHANGE)` operation through GNU `mv -T --exchange --no-copy`.
The stable path stays populated even if the process is killed at publication;
failed staging or exchange retains the previous copy.
Refresh uses GNU coreutils 9.5+ or, on an older host, an already-present isolated
Python 3 as a stdlib-only adapter to the same renameat2 syscall. Modern hosts
need no host Python. If neither adapter or filesystem exchange support is
available, preparation fails with the existing copy intact; it never falls back to a two-rename gap. Initial installation and unchanged copies
do not need the exchange option. Chromium canonicalizes an unpacked folder’s
path, so swapping a folder symlink would pin the browser to an old generation.
The browser reads an update at its next start. No browser preference, policy or
extension-installation database is edited.

`-Dstore_listing_public=false` is the default. It selects **Add from Cassini**.
When the listing is public, building with `true` selects the existing store
entry point and leaves local setup as a secondary action. Copying the folder or
opening a browser never marks setup complete: Done requires the actual native
hello/status handshake. Both entry points remain hidden with an unavailable
Flatpak bridge and disabled without a supported browser.

## Explicit browser navigation

D39.1’s **Copy Address and Open {browser}** action copies the browser’s Extensions
address and opens a new `about:blank` tab in that browser. The user pastes into
the address bar and presses Enter. Chrome/Chromium use `chrome://extensions/`,
Edge uses `edge://extensions/`, and [Brave uses `brave://extensions/`](https://support.brave.app/hc/en-us/articles/360035025231-What-extensions-are-built-into-Brave).
Chromium rejects internal URLs passed on the command line; the app never types
into the browser or edits a profile/policy to bypass that restriction.

The address is copied before launch. A worker checks early process exit for up
to 0.5 seconds natively or 2 seconds through Flatpak. The host bridge reports
early nonzero exit status instead of immediately returning success from a
background command; a browser still running after the bounded probe is detached
and survives the app. A launch failure keeps the copied address available and
shows the published inline failure line with Try again. A failed stable-folder
preparation likewise shows its true failure and retry action. All copy comes
from coordinator catalog 9ebdad477452046a54284df5be1c7b4d89d7643b verbatim.

The real-browser gate activates that production button, verifies the resulting
clipboard and actual new blank tab, then uses native address-bar keys, Developer
mode, Load unpacked and the native picker’s Open mnemonic. It never installs via
an extension-loading flag, a CDP install API, a profile edit or a test-only
address-copy path. A strictly newer private synthetic bundle checks stable-folder
updates after app/browser restart; the shipping extension is 0.2.9.

## Validation

The source tests cover pinned identity, newer-only/idempotent writes, unsafe
files, partial staging, failed exchanges, SIGTERM/SIGKILL immediately before/after
publication, concurrent browser readers and versions, a busy owner, crash-released kernel lock, a host-data path, and a minimal
host PATH without Python or diffutils. The lock file remains in place so
all contenders use the same inode; there is no PID-based stale-lock cleanup.
The installed Flatpak bridge smoke also checks the real host-data copy and unchanged inode/timestamp on another launch.

The GUI test uses the existing private display harness. The fresh-browser proof
uses `tests/recording/run.sh` with a private Xvfb, D-Bus, Pulse server, XDG roots
and browser profile. It needs ImageMagick, at-spi2-core and gir1.2-atspi-2.0 in
addition to the existing recording dependencies:

```sh
CASSINI_TEST_BUILD="$PWD/_build" tests/recording/run.sh node tests/recording/companion-install.mjs
```

The Recording integration gate runs this proof against its exact build product.
All Chrome/Chromium executable names resolve to the private wrapper, and the
selected executable is asserted before opening anything, so a runner’s installed
browser cannot divert the test into a second profile.
The accessibility probe waits for Chromium’s named native address field to
receive focus and accept its paste before Enter; clipboard ownership is checked
before either paste; it never substitutes debug-protocol navigation
for that first installation. The folder chooser is exercised with both GTK3
and GTK4 installed: its real Open mnemonic works with either toolkit, including
the GTK3 chooser that Chromium does not expose through its browser AT-SPI tree.
Successful acceptance still requires the exact extension ID/version, native
handshake and the same-folder update after restarting the browser.

It retains the real guide screenshot, native picker/extension identity/version,
connection evidence and stderr, and fails on Gtk/Adw warnings or criticals. No
Chrome Web Store request is permitted. The user's desktop and browser profile
are never used.
