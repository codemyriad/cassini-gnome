# Contributing

Thanks for helping. Issues and pull requests are welcome on
[GitHub](https://github.com/codemyriad/cassini-gnome).

## Reporting bugs

Include your distribution, how you installed Cassini (Flatpak, .deb, source)
and, if you can share it, a file that shows the problem. A file that will not
open is often explained by the format's reference reader:
`tools/cassini-extract.py file.opus` in
[cassini-format](https://github.com/codemyriad/cassini-format).

## Development

```sh
meson setup _build --prefix="$PWD/_install" -Dprofile=development
meson install -C _build
GSETTINGS_SCHEMA_DIR=_install/share/glib-2.0/schemas _install/bin/cassini-viewer
```

The `development` profile uses the application ID
`com.gocassini.Viewer.Devel`, so it can run next to an installed release.

Layout:

| Path | What |
|---|---|
| `src/cassini_viewer/portable.py` | the format reader and audio digest; no GTK |
| `src/cassini_viewer/transcript.py` | turns, interjections, search, export; no GTK |
| `src/cassini_viewer/summary.py` | the summary Markdown subset; no GTK |
| `src/cassini_viewer/player.py` | GStreamer playback |
| `src/cassini_viewer/window.py`, `src/ui/` | the window |
| `data/` | desktop file, AppStream metadata, GSettings schema, icons, man page |
| `build-aux/` | Flatpak manifests and the Debian build container |
| `debian/` | Debian packaging |

## Checks

CI runs all of these on every pull request:

```sh
ruff check src tests
ruff format --check src tests
pytest
meson test -C _build        # also validates the desktop, AppStream and schema files
```

The GTK-free modules have unit tests; please add tests with changes there.
For UI changes, follow the [GNOME Human Interface Guidelines](https://developer.gnome.org/hig/)
and include a screenshot in the pull request.

## Translations

Strings are marked with gettext. To add a language, add its code to
`po/LINGUAS` and create `po/<code>.po` from the template:

```sh
meson compile -C _build cassini-viewer-pot
msginit -i po/cassini-viewer.pot -o po/<code>.po -l <code>
```

## Releases

1. Update the version in `meson.build`, add a `<release>` to
   `data/com.gocassini.Viewer.metainfo.xml.in` and an entry to
   `debian/changelog`.
2. Tag `vX.Y.Z` and push the tag. The release workflow builds the .deb and
   the Flatpak bundle and attaches them to a GitHub release.

## License

By contributing you agree that your contributions are licensed under the
GPL-3.0-or-later, like the rest of the project.
