# Installed build checks

Meson installs `build-info.json` beside the application modules. Its content ID
covers the configured launcher, Python modules, runtime data, resources and the
bundled extension. It changes for a same-version patch as well as a version bump.
The launcher freezes this identity before importing the application.

On activation, opening a file or finishing a recording, a single worker compares
that frozen identity with the installed one. A native installation must match
all of the new manifest's file hashes before it can restart. A Flatpak queries
the current host deployment through its existing host bridge, rather than its
old `/app` mount. A partial installation retries for a bounded interval.

With no active capture, the launcher executes the installed build after the
old application shuts down. Window visibility, incoming files, automatic
recording and pending writing-down work are carried across. Starting, stopping,
saving and changing recording sources all defer restart. While capture is
active, the restart banner is visible and its action is disabled.

Native host registrations are checked on startup, before setup and during
disconnected status polling. Missing targets and executable relays belonging to
an uninstalled Flatpak are repaired. Source and Devel builds preserve healthy
foreign registrations until an explicit setup request. Unchanged manifests are
not rewritten. Native writes use a directory lock; host writes additionally
compare the previous bytes under that lock to avoid stealing a concurrent
registration.

`tests/build-refresh/run.sh PACKAGE.deb OUTPUT` installs the real package in a
private Ubuntu 26.04 container, creates two **test-only** same-version package
patches and performs actual `dpkg` upgrades. It checks fresh Python startup
identities, automatic recording with no window, real microphone frames, deferred
restart, finalized WAV counts, dead-relay repair and source/Devel ownership.
The private display, bus, home and generated PulseAudio inputs never use the
host desktop. CI uploads diagnostics and screenshots, not the derived packages.
Unit tests cover partial installs, malformed identities, broker activity races,
coalesced requests and registration publication races.
