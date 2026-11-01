# Localization (D50, D50.1 and D50.2)

Cassini talk supports English, Italian, French, Spanish, Brazilian Portuguese
and European Portuguese. `i18n.select_language()` takes GLib's ordered language
names. Bare `pt` and Brazil select `pt-BR`; every other Portuguese region
selects `pt-PT`. Unsupported preferences fall through to the next supported
language, then English. Gettext uses the same selection as the shared catalog.

UI selection never changes `LC_TIME` or `LC_NUMERIC`. Calendar names and clock
style come from the platform's formatting locale. Markdown uses those calendar
names and the short clock, with the catalog's `export.markdown.*` headings.
Transcript content, saved speaker names and notes are exported as stored.

The CLDR cardinal rules for these six languages live in `i18n.py`. They include
French and Brazilian Portuguese zero, European Portuguese zero, visible decimal
places and `many` at whole millions. Search independently pluralizes its places
and meeting counts. No runtime Babel/ICU dependency is needed.

Shared copy lives in `src/cassini_talk/strings.json`; do not edit translations
there by hand. While DrMeyers is unavailable the coordinator's authorized source
is `/mnt/ai/cassini/i18n/`: copy its `strings.json`, run its `merge.py` on the copy,
then append the exact entries in `pending/d52-strings.json` and
`pending/d50-strings.json`. Preserve every entry and do not invent missing copy.
`tests/ux/UPSTREAM.json` records every input hash and the resulting catalog hash.
Re-sync the same published bytes from coordination when DrMeyers returns.

GNOME-only messages, GtkBuilder text and desktop/AppStream descriptions live in
`po/{en,it,fr,es,pt_BR,pt_PT}.po`. Follow [the shared glossary](i18n-glossary.md).
Update `po/POTFILES.in` when adding translatable source files. Meson's
`cassini-talk-pot` / `cassini-talk-update-po` targets support translator updates;
`msgfmt --check --check-format` must pass. Runtime lookup uses the selected UI
language even for a bare Portuguese system preference.

`tests/test_i18n.py` checks every catalog id in all six languages, placeholder
agreement, all PO messages, CLDR boundary cases, language preference order and
independent Search counts. Meson checks all six generated desktop descriptions,
AppStream summaries and compiled message catalogs. The shared D50.2 Markdown
cases are pinned in `tests/export/UPSTREAM.json`; their date placeholder is
compared to GLib's platform formatter, keeping the rest byte-for-byte exact.

The board gate launches the installed app in French and Brazilian Portuguese
using `CASSINI_SCREENSHOT_LANGUAGE`, with the formatting locale held at en_GB.
It observes mapped widget text and real gettext menu labels, validates the
1120×720 PNGs and rejects GTK warnings/criticals. The existing private Xvfb,
D-Bus, HOME and audio isolation remain in effect. No fixed readiness delay is
added; snapshots wait for the scan and rendered frames under a failure deadline.
