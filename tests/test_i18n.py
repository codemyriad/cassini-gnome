# SPDX-License-Identifier: GPL-3.0-or-later
"""D50: every shared/app-only template, GLib preferences and independent plurals."""

import ast
import configparser
import gettext
import locale
import os
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from decimal import Decimal
from pathlib import Path
from string import Formatter

import pytest

from cassini_talk import i18n, strings, ux_formats

ROOT = Path(__file__).parents[1]
TAGS = dict(zip(i18n.LANGUAGES, ("en", "it", "fr", "es", "pt_BR", "pt_PT"), strict=True))


def fields(value):
    return {field for _literal, field, _spec, _conversion in Formatter().parse(value) if field is not None}


@pytest.mark.parametrize("lang", i18n.LANGUAGES)
def test_every_catalog_entry_renders_in_each_language_with_matching_placeholders(lang):
    with i18n.using_language(lang):
        for identifier, entry in strings.ENTRIES.items():
            translated = (
                dict(text=entry["en"], **({"plural": entry["plural"]} if "plural" in entry else {}))
                if lang == "en"
                else entry["l10n"][lang]
            )
            assert translated["text"].strip(), (lang, identifier)
            assert fields(translated["text"]) == fields(entry["en"]), (lang, identifier)
            values = dict.fromkeys(fields(entry["en"]), 2)
            assert strings.text(identifier, **values) == translated["text"].format(**values)
            if "mac" in entry and lang != "en":
                assert fields(translated["mac"]) == fields(entry["mac"]), (lang, identifier, "mac")
            if "plural" not in entry:
                continue
            forms = translated["plural"]
            assert {"one", "other"} <= forms.keys(), (lang, identifier)
            allowed = fields(entry["en"]) | set().union(*(fields(v) for v in entry["plural"].values()))
            for category, template in forms.items():
                assert template.strip() and fields(template) <= allowed, (lang, identifier, category)
                if category != "one":
                    assert fields(template) == fields(entry["plural"]["other"]), (lang, identifier, category)
            for count in (0, 1, 2, 1_000_000):
                rendered = strings.plural(identifier, count, **dict.fromkeys(allowed - {"n"}, 2))
                assert rendered.strip(), (lang, identifier, count)


@pytest.mark.parametrize(
    "names,expected",
    [
        ([], "en"),
        (["de_DE.UTF-8", "C"], "en"),
        (["de_DE", "fr_CA.UTF-8", "it"], "fr"),
        (["en_US.UTF-8", "fr"], "en"),
        (["it_IT@euro"], "it"),
        (["es_MX"], "es"),
        (["pt"], "pt-BR"),
        (["pt_BR.UTF-8", "pt"], "pt-BR"),
        (["pt-PT", "pt"], "pt-PT"),
        (["pt_AO.UTF-8", "pt"], "pt-PT"),
        (["pt-MZ"], "pt-PT"),
        (["pt-Latn-BR"], "pt-BR"),
        (["pt-Latn"], "pt-BR"),
        (["FR-fr"], "fr"),
    ],
)
def test_language_preference_order_and_portuguese_region(names, expected):
    assert i18n.select_language(names) == expected


@pytest.mark.parametrize(
    "lang,count,expected",
    [
        ("en", 0, "other"),
        ("en", 1, "one"),
        ("en", Decimal("1.0"), "other"),
        ("it", 0, "other"),
        ("it", 1, "one"),
        ("it", Decimal("1.0"), "other"),
        ("es", Decimal("1.0"), "one"),
        ("es", Decimal("1.1"), "other"),
        ("fr", 0, "one"),
        ("fr", Decimal("1.9"), "one"),
        ("fr", 2, "other"),
        ("pt-BR", 0, "one"),
        ("pt-BR", Decimal("0.5"), "one"),
        ("pt-BR", 1, "one"),
        ("pt-PT", 0, "other"),
        ("pt-PT", 1, "one"),
        ("pt-PT", Decimal("1.0"), "other"),
        ("fr", 1_000_000, "many"),
        ("it", 2_000_000, "many"),
        ("es", 1_000_000, "many"),
        ("pt-BR", 1_000_000, "many"),
        ("pt-PT", 1_000_000, "many"),
        ("en", 1_000_000, "other"),
        ("fr", Decimal("1000000.0"), "other"),
        ("it", -1, "one"),
    ],
)
def test_cldr_cardinal_categories(lang, count, expected):
    assert i18n.plural_category(count, lang=lang) == expected


@pytest.mark.parametrize(
    "lang,zero,one,two",
    [
        ("en", "0 places in 2 meetings", "1 place in 2 meetings", "2 places in 1 meeting"),
        ("it", "0 risultati in 2 riunioni", "1 risultato in 2 riunioni", "2 risultati in 1 riunione"),
        ("fr", "0 occurrence dans 2 réunions", "1 occurrence dans 2 réunions", "2 occurrences dans 1 réunion"),
        ("es", "0 coincidencias en 2 reuniones", "1 coincidencia en 2 reuniones", "2 coincidencias en 1 reunión"),
        ("pt-BR", "0 trecho em 2 reuniões", "1 trecho em 2 reuniões", "2 trechos em 1 reunião"),
        ("pt-PT", "0 ocorrências em 2 reuniões", "1 ocorrência em 2 reuniões", "2 ocorrências em 1 reunião"),
    ],
)
def test_search_counts_pluralize_places_and_meetings_independently(lang, zero, one, two):
    with i18n.using_language(lang):
        assert ux_formats.search_count(0, 2) == zero
        assert ux_formats.search_count(1, 2) == one
        assert ux_formats.search_count(2, 1) == two


def test_ui_selection_does_not_mutate_time_or_number_locale(monkeypatch, tmp_path):
    before = locale.setlocale(locale.LC_TIME), locale.setlocale(locale.LC_NUMERIC)
    domain, directory = gettext.textdomain(), gettext.bindtextdomain("cassini-talk")
    monkeypatch.setenv("LANGUAGE", "en_GB")
    monkeypatch.setattr(i18n, "_localedir", i18n._localedir)
    try:
        with i18n.using_language("pt-BR"):
            i18n.configure(str(tmp_path))
            assert strings.text("common.record") == "Gravar"
        assert (locale.setlocale(locale.LC_TIME), locale.setlocale(locale.LC_NUMERIC)) == before
    finally:
        gettext.textdomain(domain)
        gettext.bindtextdomain("cassini-talk", directory)


def po_entries(path):
    entries, entry, key = {}, {}, None
    for line in path.read_text().splitlines() + [""]:
        if not line:
            if entry.get("msgid"):
                entries[entry["msgid"]] = entry
            entry, key = {}, None
        elif match := re.match(r'(msgid_plural|msgid|msgstr(?:\[\d+\])?) (".*")$', line):
            key = match[1]
            entry[key] = ast.literal_eval(match[2])
        elif line.startswith('"') and key:
            entry[key] += ast.literal_eval(line)
    return entries


@pytest.mark.parametrize("lang", i18n.LANGUAGES)
def test_all_gnome_po_messages_are_translated_with_matching_placeholders(lang):
    english = po_entries(ROOT / "po/en.po")
    translated = po_entries(ROOT / "po" / (TAGS[lang] + ".po"))
    assert set(translated) == set(english)
    for identifier, entry in translated.items():
        templates = [entry["msgstr[0]"], entry["msgstr[1]"]] if "msgid_plural" in entry else [entry["msgstr"]]
        for template in templates:
            assert template.strip(), (lang, identifier)
            assert fields(template) == fields(identifier), (lang, identifier)


@pytest.mark.parametrize("lang", i18n.LANGUAGES)
def test_po_compiles_and_translates_actual_gnome_messages(lang, tmp_path):
    if not (compiler := shutil.which("msgfmt")):
        pytest.skip("gettext compiler is a build dependency")
    path = ROOT / "po" / (TAGS[lang] + ".po")
    destination = tmp_path / "messages.mo"
    subprocess.run([compiler, "--check", "--check-format", "-o", str(destination), str(path)], check=True)
    with destination.open("rb") as stream:
        messages = gettext.GNUTranslations(stream)
    assert (
        messages.gettext("Open…")
        == {"en": "Open…", "it": "Apri…", "fr": "Ouvrir…", "es": "Abrir…", "pt-BR": "Abrir…", "pt-PT": "Abrir…"}[lang]
    )
    assert (
        messages.ngettext("{n} match", "{n} matches", 0)
        == {
            "en": "{n} matches",
            "it": "{n} risultati",
            "fr": "{n} occurrence",
            "es": "{n} coincidencias",
            "pt-BR": "{n} resultado",
            "pt-PT": "{n} ocorrências",
        }[lang]
    )


@pytest.mark.parametrize(
    "names,expected", [("fr_FR:it", "fr"), ("pt", "pt-BR"), ("pt_AO:pt", "pt-PT"), ("de_DE", "en")]
)
def test_real_glib_language_names_in_a_fresh_process(names, expected):
    result = subprocess.run(
        [sys.executable, "-c", "from cassini_talk.i18n import language; print(language())"],
        env=dict(os.environ, LANGUAGE=names, LC_ALL="C.UTF-8", PYTHONPATH=str(ROOT / "src")),
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == expected


def test_generated_desktop_metainfo_and_mo_cover_all_six_languages():
    if not (directory := os.environ.get("CASSINI_TEST_BUILD")):
        pytest.skip("Meson build outputs are checked by the build/package gates")
    build = Path(directory)
    desktop = next((build / "data").glob("*.desktop"))
    metadata = next((build / "data").glob("*.metainfo.xml"))
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    parser.read(desktop)
    app = parser["Desktop Entry"]
    summaries = {
        node.attrib.get("{http://www.w3.org/XML/1998/namespace}lang", "en"): node.text
        for node in ET.parse(metadata).findall("summary")
    }
    for language, tag in TAGS.items():
        for field in ("GenericName", "Comment", "Keywords"):
            assert app[f"{field}[{tag}]"].strip(), (field, tag)
        assert app[f"Keywords[{tag}]"].endswith(";")
        assert summaries[language].strip(), language
        assert (build / "po" / tag / "LC_MESSAGES/cassini-talk.mo").is_file(), tag


def test_po_only_changes_run_translated_board_gate():
    import runpy

    changes = runpy.run_path(str(ROOT / "build-aux/ci/changes.py"))
    assert changes["select"](["po/fr.po"])["boards"]


def test_background_worker_finds_its_prefix_catalog_without_a_ui_launcher(tmp_path, monkeypatch):
    if not (compiler := shutil.which("msgfmt")):
        pytest.skip("gettext compiler is a build dependency")
    destination = tmp_path / "share/locale/fr/LC_MESSAGES/cassini-talk.mo"
    destination.parent.mkdir(parents=True)
    subprocess.run([compiler, "-o", str(destination), str(ROOT / "po/fr.po")], check=True)
    monkeypatch.setattr(i18n, "__file__", str(tmp_path / "share/cassini-talk/cassini_talk/i18n.py"))
    monkeypatch.setattr(i18n, "_localedir", None)
    with i18n.using_language("fr"):
        assert i18n.gettext("Open…") == "Ouvrir…"
        assert i18n.gettext("Recovered recording") == "Enregistrement récupéré"
