# SPDX-License-Identifier: GPL-3.0-or-later
"""L-27 first-run hero (D-972): wave geometry, catalog copy and the GNOME layout contract."""

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


def test_wave_uses_square_4px_bars_that_fit_the_width(hero):
    assert hero.BAR_WIDTH_PX == 4 and hero.MIN_BAR_PX == 4
    assert hero.bar_count(0) == 0
    assert hero.bar_count(4) == 1
    assert hero.WAVE_BARS == 64 and hero.WAVE_HEIGHT_PX == 56
    assert hero.bar_count(hero.WAVE_WIDTH_PX) == 64
    for width in (8, 60, 360, 1000):
        n = hero.bar_count(width)
        assert n * hero.BAR_WIDTH_PX + (n - 1) * hero.BAR_GAP_PX <= width


def test_wave_heights_are_deterministic_and_bounded(hero):
    heights = hero.bar_heights(64, 56)
    assert heights == hero.bar_heights(64, 56)
    assert all(hero.MIN_BAR_PX <= h <= 56 for h in heights)
    assert len(set(heights)) > 5, "a flat line does not read as a wave"
    assert hero.bar_heights(0) == []


def test_wave_fades_at_both_ends_on_desktop(hero):
    alphas = hero.bar_alphas(64)
    assert alphas == alphas[::-1], "desktop fades symmetrically at both ends"
    assert alphas[0] < 0.2 and alphas[-1] < 0.2
    assert max(alphas) == 1.0 and alphas[31] == 1.0
    half = alphas[:32]
    assert half == sorted(half)
    assert hero.bar_alphas(0) == [] and hero.bar_alphas(1) == [1.0]


def test_hero_copy_comes_from_the_pinned_catalog():
    catalog = json.loads((ROOT / "src/cassini_talk/strings.json").read_text())
    by_id = {e["id"]: e for e in catalog["entries"]}
    assert by_id["firstRun.hero.title"]["en"] == "Every word written down."
    assert by_id["firstRun.hero.subtitle"]["en"] == "None of it uploaded."
    for key in ("firstRun.hero.title", "firstRun.hero.subtitle"):
        assert "gnome" in by_id[key]["platforms"]
        for lang in ("it", "fr", "es", "pt-BR", "pt-PT"):
            assert by_id[key]["l10n"][lang]["text"].strip()


def test_first_run_view_follows_harmony_section_10():
    source = (ROOT / "src/cassini_talk/library_window.py").read_text()
    block = source[source.index("    def _first_run_header(self):") : source.index('self.root_box.add_named(view, "empty")')]
    # Desktop hero: no phone backup qualifier, no legacy title or shortcut hint, no header Record.
    assert "phoneBackupQualifier" not in block and "subtitlePhoneNote" not in block
    assert '"firstRun.title"' not in block and "firstRun.shortcutHint" not in block
    assert "capture_controls" not in block and "_header(record=True)" not in block
    for key in ("firstRun.hero.title", "firstRun.hero.subtitle", "firstRun.body", "firstRun.footer", "library.title"):
        assert f'"{key}"' in block, key
    assert "AccessibleRole.HEADING" in block
    assert "BrandMark(HERO_MARK_HEIGHT_PX)" in block
    # Record sits at the start of the wave.
    assert block.index("wave_row.append(record)") < block.index("wave_row.append(self.hero_wave)")


def test_hero_styles_read_brand_roles_only():
    css = (ROOT / "src/style.css").read_text()
    rules = dict(re.findall(r"\.library-window \.(hero-[\w-]+) \{([^}]*)\}", css))
    assert "font-size: 64px" in rules["hero-title"] and "font-weight: 700" in rules["hero-title"]
    assert "var(--cx-accent)" in rules["hero-wave"]
    assert "var(--cx-ink-3)" in rules["hero-subtitle"]
    assert "line-height: 0.98" in rules["hero-title"]
    for body in rules.values():
        assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(", body)
