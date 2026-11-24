# SPDX-License-Identifier: GPL-3.0-or-later
"""D-1140: the GTK 4.14 (Ubuntu 24.04) translation of src/style.css. No gi."""

import re
from pathlib import Path

import pytest

from cassini_talk import css_compat, runtime, shortcuts_table

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"


def app_css():
    return css_compat.inline_imports((SRC / "style.css").read_text(), lambda name: (SRC / name).read_text())


def tokens(dark):
    """--cx-* values of src/tokens.css for one scheme."""
    text = (SRC / "tokens.css").read_text()
    light_block, dark_block = re.findall(r":root\s*\{(.*?)\}", text, flags=re.S)[:2]
    values = dict(re.findall(r"(--cx-[a-z0-9-]+):\s*([^;]+);", light_block))
    if dark:
        values.update(re.findall(r"(--cx-[a-z0-9-]+):\s*([^;]+);", dark_block))
    return values


def test_only_gtk_before_4_16_needs_the_translation():
    assert css_compat.is_legacy((4, 14, 5))
    assert css_compat.is_legacy((4, 15))
    assert not css_compat.is_legacy((4, 16, 0))
    assert not css_compat.is_legacy((4, 20, 1))
    # The runtime floor is the oldest GTK the translation serves.
    assert css_compat.is_legacy(runtime.GTK)
    assert runtime.GTK == (4, 14) and runtime.ADW == (1, 5)


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
def test_app_stylesheet_has_nothing_gtk_4_14_cannot_parse(dark):
    out = css_compat.legacy_stylesheet(app_css(), dark)
    assert "var(" not in out
    assert "color-mix(" not in out
    assert "@media" not in out and "@import" not in out and ":root" not in out
    assert not re.search(r"^\s*--", out, flags=re.M), "custom property declarations remain"
    # Every brace closes and every rule keeps declarations.
    assert out.count("{") == out.count("}")
    assert "{\n\n}" not in out


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
def test_palette_overrides_become_libadwaita_1_5_named_colours(dark):
    out = css_compat.legacy_stylesheet(app_css(), dark)
    cx = tokens(dark)
    assert f"@define-color window_bg_color {cx['--cx-ground']};" in out
    assert f"@define-color view_bg_color {cx['--cx-surface']};" in out
    assert f"@define-color accent_bg_color {cx['--cx-accent-strong']};" in out
    assert f"@define-color destructive_bg_color {cx['--cx-error-fill']};" in out


def test_dark_tokens_and_dark_media_rules_apply_only_to_the_dark_sheet():
    light, dark = (css_compat.legacy_stylesheet(app_css(), scheme) for scheme in (False, True))
    assert tokens(False)["--cx-ground"] != tokens(True)["--cx-ground"]
    assert f"window_bg_color {tokens(True)['--cx-ground']};" in dark
    assert f"window_bg_color {tokens(True)['--cx-ground']};" not in light
    css = ":root { --a: #000000; } x { color: var(--a); } @media (prefers-color-scheme: dark) { :root { --a: #ffffff; } y { color: red; } }"
    assert css_compat.legacy_stylesheet(css, False) == "x {\n  color: #000000;\n}\n"
    assert css_compat.legacy_stylesheet(css, True) == "x {\n  color: #ffffff;\n}\ny {\n  color: red;\n}\n"


def test_color_mix_is_computed_like_css():
    css = ":root { --ink: #191B1F; --tint: #FBE4E6; } a { color: color-mix(in srgb, var(--ink) 10%, transparent); }"
    css += " b { color: color-mix(in srgb, #000000 50%, #ffffff); } c { color: color-mix(in srgb, #ff0000, #0000ff); }"
    css += " d { color: color-mix(in srgb, @accent_color 30%, @window_bg_color); }"
    css += " e { color: color-mix(in srgb, var(--accent-color) 25%, transparent); }"
    out = css_compat.legacy_stylesheet(css)
    # Premultiplied: a transparent partner keeps the colour and scales alpha.
    assert "a {\n  color: rgba(25, 27, 31, 0.1);\n}" in out
    assert "b {\n  color: rgb(128, 128, 128);\n}" in out
    assert "c {\n  color: rgb(128, 0, 128);\n}" in out
    # Named colours fall back to GTK's mix()/alpha(): mix(b, a, f) = a*f + b*(1-f).
    assert "d {\n  color: mix(@window_bg_color, @accent_color, 0.3);\n}" in out
    # An undefined libadwaita palette variable reads the 1.5 named colour.
    assert "e {\n  color: alpha(@accent_color, 0.25);\n}" in out


def test_unresolvable_and_scoped_custom_properties_are_dropped():
    css = ":root { --a: #111111; color: var(--a); } toast { --accent-color: red; color: var(--a); }"
    css += " x { margin: var(--nope); padding: 2px; } y { opacity: var(--missing); }"
    out = css_compat.legacy_stylesheet(css)
    assert out == "window {\n  color: #111111;\n}\ntoast {\n  color: #111111;\n}\nx {\n  padding: 2px;\n}\n"


def test_var_fallbacks_and_nested_references():
    css = ":root { --a: var(--b); --b: var(--c, #222222); } x { color: var(--a); border-color: var(--z, var(--b)); }"
    assert css_compat.legacy_stylesheet(css) == "x {\n  color: #222222;\n  border-color: #222222;\n}\n"


def test_shortcut_rows_come_from_the_dialog_ui():
    found = shortcuts_table.sections((SRC / "shortcuts-dialog.ui").read_text())
    assert [section for section, _items in found] == list(shortcuts_table.GROUPS)
    for section, items in found:
        assert tuple(name for name, _accel in items) == shortcuts_table.GROUPS[section][1]
        assert all(accelerator for _name, accelerator in items)
    assert dict(found[1][1])["listen_turns"] == "<Alt>Down <Alt>Up"
