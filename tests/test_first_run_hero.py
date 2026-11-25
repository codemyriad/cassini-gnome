# SPDX-License-Identifier: GPL-3.0-or-later
"""D105 first run (L-70, L-71; D-1098): wave geometry, catalog copy and the GNOME layout contract."""

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def hero():
    gi = pytest.importorskip("gi")
    try:
        gi.require_version("Gtk", "4.0")
    except ValueError:
        pytest.skip("GTK 4 introspection is unavailable")
    from cassini_talk import first_run_hero

    return first_run_hero


def test_wave_is_the_drawn_conversation(hero):
    # D105 §5: 3 px bars as tall as the 40 px mark, the bars drawn on P2FirstRun-Desktop.
    assert hero.BAR_WIDTH_PX == 3 and hero.BAR_GAP_PX == 3 and hero.WAVE_HEIGHT_PX == 40
    assert hero.WAVE_BARS == 84 == len(hero.DRAWN_HEIGHTS) == len(hero.DRAWN_ALPHAS)
    assert hero.WAVE_WIDTH_PX == 84 * 3 + 83 * 3
    assert all(6 <= h <= hero.WAVE_HEIGHT_PX for h in hero.DRAWN_HEIGHTS)
    assert all(0.5 <= a <= 1.0 for a in hero.DRAWN_ALPHAS)
    assert hero.bars() == list(zip(hero.DRAWN_HEIGHTS, hero.DRAWN_ALPHAS, strict=True))


def test_wave_reads_as_four_phrases_and_three_pauses(hero):
    # A pause drops to short bars (<= 8 px) for at least two bars in a row and dims below full strength.
    heights, alphas = hero.DRAWN_HEIGHTS, hero.DRAWN_ALPHAS
    body = range(len(heights) - hero.FADE_TAIL_BARS)
    pauses, run = [], []
    for i in body:
        if alphas[i] < 1.0:
            run.append(i)
        elif run:
            pauses.append(run)
            run = []
    pauses += [run] if run else []
    assert len(pauses) == 3, pauses
    for pause in pauses:
        assert min(heights[i] for i in pause) <= 6, pause
    # One sharp cut (short, a single dimmed step), two easing in and out (symmetric fades).
    lengths = sorted(len(p) for p in pauses)
    assert lengths[0] == 3 and lengths[1] >= 5
    # Fading out at the end: strictly falling alphas, ending at half strength on the shortest bars.
    tail = alphas[-hero.FADE_TAIL_BARS :]
    assert list(tail) == sorted(tail, reverse=True) and tail[-1] == 0.5
    assert heights[-6:] == (6,) * 6


def test_narrow_wave_keeps_whole_bars_and_the_fade_out(hero):
    assert hero.bar_count(0) == 0 and hero.bar_count(3) == 1
    assert hero.bar_count(hero.WAVE_WIDTH_PX) == hero.WAVE_BARS
    assert hero.bar_count(10_000) == hero.WAVE_BARS, "never more bars than drawn"
    for width in (3, 60, 200, hero.MIN_WAVE_WIDTH_PX, 400):
        n = hero.bar_count(width)
        assert n * hero.BAR_WIDTH_PX + (n - 1) * hero.BAR_GAP_PX <= width
        shown = hero.bars(n)
        assert len(shown) == n
        assert shown[-min(n, hero.FADE_TAIL_BARS) :] == hero.bars()[-min(n, hero.FADE_TAIL_BARS) :]
    assert hero.bars(0) == []
    assert hero.bar_count(hero.MIN_WAVE_WIDTH_PX) == hero.MIN_WAVE_BARS


def test_hero_copy_comes_from_the_pinned_catalog():
    catalog = json.loads((ROOT / "src/cassini_talk/strings.json").read_text())
    by_id = {e["id"]: e for e in catalog["entries"]}
    # L-70 (D105 §2): "written down" stays only here, as brand voice.
    assert by_id["firstRun.hero.title"]["en"] == "Every word written down."
    assert by_id["firstRun.hero.subtitle.computer"]["en"] == "Right here, on this computer."
    assert by_id["firstRun.privacy.computer"]["en"] == "Transcribed on this computer, kept in {folder}"
    for key in ("firstRun.hero.title", "firstRun.hero.subtitle.computer", "firstRun.privacy.computer"):
        assert "gnome" in by_id[key]["platforms"]
        for lang in ("it", "fr", "es", "pt-BR", "pt-PT"):
            assert by_id[key]["l10n"][lang]["text"].strip()
            if key == "firstRun.privacy.computer":
                assert by_id[key]["l10n"][lang]["text"].count("{folder}") == 1, lang


def test_first_run_view_follows_harmony_section_10():
    source = (ROOT / "src/cassini_talk/library_window.py").read_text()
    block = source[
        source.index("    def _first_run_header(self):") : source.index('self.root_box.add_named(view, "empty")')
    ]
    # Desktop hero: no phone backup qualifier, no legacy title or shortcut hint, no header Record.
    assert "phoneBackupQualifier" not in block and "subtitlePhoneNote" not in block
    assert '"firstRun.title"' not in block and "firstRun.shortcutHint" not in block
    assert "capture_controls" not in block and "_header(record=True)" not in block
    # L-60: the header carries ☰ and Import audio…; the superseded copy is gone.
    assert "self.import_button()" in block and "header.pack_start(self._primary_menu_button())" in block
    assert '"firstRun.body"' not in block and '"firstRun.footer"' not in block
    # D105: no how-to line on desktop; the new hero line and privacy line replace the superseded ones.
    for gone in (
        '"firstRun.howTo"',
        '"firstRun.hero.subtitle"',
        '"firstRun.libraryLocation"',
        '"privacy.onDevice.computer"',
    ):
        assert gone not in block, gone
    for key in (
        "firstRun.hero.title",
        "firstRun.hero.subtitle.computer",
        "firstRun.shortcutStarts",
        "common.shortcut.record",
        "firstRun.privacy.computer",
        "firstRun.openFolder.tooltip",
        "firstRun.changeFolder",
        "firstRun.changeFolder.tooltip",
        "library.title",
    ):
        assert f'"{key}"' in block, key
    assert "AccessibleRole.HEADING" in block
    assert "BrandMark(HERO_MARK_HEIGHT_PX)" in block
    # The column is capped at 860 px (as drawn) by a clamp, never forced as a minimum width.
    assert "Adw.Clamp(maximum_size=860" in block and "width_request=" not in block
    # One edge: the mark then the wave on one row; Record then its shortcut on another.
    assert block.index("mark_row.append(mark)") < block.index("mark_row.append(self.hero_wave)")
    assert block.index("record_row.append(record)") < block.index("record_row.append(self.hero_shortcut)")
    # The shield is a drawn glyph, not the old padlock; the pencil reuses Preferences' Move… flow.
    assert "changes-prevent-symbolic" not in block and 'LineGlyph("shield"' in block
    assert "choose_library_folder()" in block and "in_flatpak" in block


def test_preferences_and_first_run_share_one_move_flow():
    settings = (ROOT / "src/cassini_talk/settings_dialog.py").read_text()
    assert "self.app.choose_library_folder(self)" in settings
    assert "select_folder(" not in settings, "the folder chooser lives once, on the application"
    main = (ROOT / "src/cassini_talk/main.py").read_text()
    flow = main[main.index("    def choose_library_folder(") : main.index("    def move_library(")]
    assert "self.move_library(" in flow and "select_folder(" in flow


def test_privacy_line_splits_the_folder_out_of_any_word_order(monkeypatch, tmp_path):
    gi = pytest.importorskip("gi")
    try:
        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
    except ValueError:
        pytest.skip("GTK 4 introspection is unavailable")
    from cassini_talk import i18n, library_location
    from cassini_talk.library_window import LIBRARY_FOLDER_LINK, LibraryWindow

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(library_location, "primary", lambda: tmp_path / "Music/Cass & <ini>")
    for lang, before in (
        ("en", "Transcribed on this computer, kept in "),
        ("fr", "Transcrit sur cet ordinateur, conservé dans "),
    ):
        with i18n.using_language(lang):
            markup = LibraryWindow._privacy_markup(None)
        assert markup.startswith(before), markup
        assert f'<a href="{LIBRARY_FOLDER_LINK}" title="' in markup
        assert "Music/Cass &amp; &lt;ini&gt;</span></a>" in markup
        assert markup.endswith("</a>"), "no full stop after the folder (D105 §4)"


def test_hero_styles_read_brand_roles_only():
    css = (ROOT / "src/style.css").read_text()
    rules = dict(re.findall(r"\.library-window \.(hero-[\w-]+) \{([^}]*)\}", css))
    assert "font-size: 48pt" in rules["hero-title"] and "font-weight: 700" in rules["hero-title"]
    # D105 §5: the wave is the hero's second-line grey, never the accent or red; the shield is the accent.
    assert "var(--cx-ink-3)" in rules["hero-wave"] and "record" not in rules["hero-wave"]
    assert "var(--cx-ink-3)" in rules["hero-subtitle"]
    assert "var(--cx-accent)" in rules["hero-shield"]
    assert "line-height: 1.08" in rules["hero-title"] and "letter-spacing: -0.03em" in rules["hero-title"]
    for body in rules.values():
        assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(", body)


def test_wave_can_shrink_below_its_drawn_width(hero):
    source = (ROOT / "src/cassini_talk/first_run_hero.py").read_text()
    assert "set_size_request" not in source, "a size request would make the drawn width a minimum"
    assert hero.bar_count(hero.MIN_WAVE_WIDTH_PX) == hero.MIN_WAVE_BARS
    assert hero.MIN_WAVE_WIDTH_PX < hero.WAVE_WIDTH_PX
