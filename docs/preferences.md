# Preferences implementation (D15)

The native four-page `Adw.PreferencesDialog` opens from the primary menu or Ctrl+comma. Search is disabled. Recording uses the same GSettings value as RecordFirst, inverted for “Explain recording before the first one”. The browser auto-record switch is owned here rather than Browser meetings; broker observation and writes run off GTK. A last explicit toggle is saved even if the dialog closes before an observation finishes.

The record-anywhere binding remains desktop-owned. The GlobalShortcuts portal is queried before binding; an existing record action is registered without a replacement preferred trigger. Change invokes ConfigureShortcuts and accepts the new description through ShortcutsChanged. GSettings caches its display description, not a parsed localized accelerator. An unavailable portal leaves Change insensitive.

Transcription shows only the bundled Whistle row, its actual installed weight-file size when available, Included and In use for new meetings. The row is hidden in unsupported/unbundled builds. It offers no future model or speaker controls. The updates bullet is absent because the application has no updater.

## Work still gated on coordination

This branch is a draft until Library relocation and generic connected-browser copy are settled (REPLIES-GNOME R18/R19). Move is not connected to capture routing yet. The standalone move primitive is tested but does not change library configuration or any host path. It renames saved meeting folders without overwrite; across filesystems it verifies copies before publishing, preserves opaque extra files/symlinks without following them, detects source changes, and retains the original on failure. The application still needs the agreed active-recording deferral, shared host destination, retained-root visibility and user-facing failure line.

## Isolated evidence

`tests/ui/settings.py` drives native Preferences against a private D-Bus portal double and real private browser broker. It verifies both first-record directions, ConfigureShortcuts/ShortcutsChanged, re-registration preserving desktop choice, automatic recording persistence, connected browser status and close during an outstanding choice. `tests/recording/app-control.mjs` uses the real extension and private Pulse container to verify discovery, recording, Stop and automatic recording through Preferences.

`build-aux/ux-boards.sh` renders settings-recording and settings-transcription from the main fixture at 1120×720, in light and dark modes. These capture the native dialog over the Library; current libadwaita uses an inline page switcher in its title bar rather than the mock-up’s separate title/tab rows. No desktop screenshot or real user audio device is accessed. Unit move tests include a real cross-filesystem transfer into a private `/dev/shm` directory, collisions, low space, publication failure, source changes and deferred folders.
