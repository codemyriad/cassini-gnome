# Localization (D50, D50.1, D50.2 and D50.3)

Cassini talk supports English, Italian, French, Spanish, Brazilian Portuguese
and European Portuguese. `i18n.select_language()` takes GLib's ordered language
names. Bare `pt` and Brazil select `pt-BR`; every other Portuguese region
selects `pt-PT`. Unsupported preferences fall through to the next supported
language, then English. Gettext uses the same selection as the shared catalog.

UI selection never changes `LC_TIME` or `LC_NUMERIC`. All words, including
weekday/month names and unit words, follow the UI language. Markdown uses that
language's CLDR full-date pattern followed by the formatting locale's short
clock, with the catalog's `export.markdown.*` headings. Decimal/grouping
separators follow `LC_NUMERIC`. For example, French UI with US formatting gives
`dimanche 1 novembre 2026, 9:00 AM`. No global locale is swapped to do this.
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
compared with independent date-word expectations and the native clock,
keeping all stored content byte-for-byte exact. `calendar_formats.py` uses only
the six pinned CLDR 49 calendar tables in `cldr-dates.json`, with source hashes
in `tests/i18n/CLDR-SOURCES.json` and the unmodified Unicode-3.0 license/notice.
No additional runtime dependency or locale-table installation is needed.

The board gate launches the installed app in French and Brazilian Portuguese
using `CASSINI_SCREENSHOT_LANGUAGE`, with the formatting locale held at en_GB.
It observes mapped widget text and real gettext menu labels, validates the
1120×720 PNGs and rejects GTK warnings/criticals. The existing private Xvfb,
D-Bus, HOME and audio isolation remain in effect. No fixed readiness delay is
added; snapshots wait for the scan and rendered frames under a failure deadline.
