# Contributing

Thanks for helping. Issues and pull requests are welcome on
[GitHub](https://github.com/codemyriad/cassini-gnome).

## Reporting bugs

Include your distribution, how you installed Cassini (Flatpak, .deb, .rpm, Arch package, source)
and, if you can share it, a file that shows the problem. A file that will not
open is often explained by the format's reference reader:
`tools/cassini-extract.py file.opus` in
[cassini-format](https://github.com/codemyriad/cassini-format).

## Development

```sh
build-aux/install-deps.sh   # build and runtime dependencies, any common distribution
make run                    # build, install into _install/ and run
```

`make help` lists the other targets. They wrap Meson and the scripts in
`build-aux/`, which you can also call directly.

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
| `build-aux/` | dependency installer, distribution test matrix, and the Flatpak, Debian, RPM and Arch package builds |
| `src/cassini_viewer/runtime.py` | the startup check for missing or old libraries; no GTK |
| `debian/` | Debian packaging |

## Checks

CI runs all of these on every pull request:

```sh
make lint                   # browser-protocol/verify.py, ruff check and ruff format --check
pytest
make test                   # meson test: also validates the desktop, AppStream and schema files
make check-distros          # build and test on six distributions, in containers
```

CI also builds the .deb, both RPMs, the Arch package and the Flatpak.

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

1. Update the version in `meson.build`, and the same version in:
   - a `<release>` in `data/com.gocassini.Viewer.metainfo.xml.in`
   - an entry in `debian/changelog`
   - `Version:` and a `%changelog` entry in `build-aux/rpm/cassini-viewer.spec`
   - `pkgver` in `build-aux/arch/PKGBUILD`

   The deb, RPM and Arch build scripts refuse to build when their version differs
   from `meson.build`.
2. Tag `vX.Y.Z` and push the tag. The release workflow builds the .deb, the
   Fedora and openSUSE RPMs, the Arch package and the Flatpak bundle, and
   attaches them to a GitHub release.

## License

By contributing you agree that your contributions are licensed under the
GPL-3.0-or-later, like the rest of the project.
