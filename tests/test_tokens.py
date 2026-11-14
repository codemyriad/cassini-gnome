# SPDX-License-Identifier: GPL-3.0-or-later
"""W1.1 sync checks: vendored brand/tokens.json, generated token files, and src/ literal scan (HARMONY 10.1)."""

import hashlib
import importlib.util
import json
import re
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from cassini_talk import tokens

ROOT = Path(__file__).resolve().parents[1]
VENDORED = ROOT / "ux/brand/tokens.json"
UPSTREAM = ROOT / "ux/brand/UPSTREAM.json"
TOKENS_CSS = ROOT / "src/tokens.css"
STYLE_CSS = ROOT / "src/style.css"
BRAND_TOKENS_PY = ROOT / "src/cassini_talk/_brand_tokens.py"
GENERATOR_PY = ROOT / "build-aux/generate-brand-tokens.py"

CSS_DECL_RE = re.compile(r"--([a-z0-9-]+)\s*:\s*([^;]+);")
HEADER_RE = re.compile(r"from codemyriad/cassini-ux@([0-9a-f]{40}):brand/tokens\.json")


def _load_generator():
    spec = importlib.util.spec_from_file_location("generate_brand_tokens", GENERATOR_PY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def parse_css_theme(
    tokens_css_text: str,
    style_css_text: str,
) -> tuple[dict[str, str], dict[str, str], dict[str, str], dict[str, str], dict[str, str]]:
    """Extract --cx-* from generated tokens.css and --app-*/adw overrides from style.css."""
    tok_top = re.search(r"(?:^|\n):root\s*\{([^}]*)\}", tokens_css_text)
    tok_dark = re.search(
        r"@media\s*\(\s*prefers-color-scheme\s*:\s*dark\s*\)\s*\{\s*:root\s*\{([^}]*)\}\s*\}",
        tokens_css_text,
    )
    assert tok_top is not None and tok_dark is not None, "missing :root blocks in generated src/tokens.css"

    cx_light = {
        raw.removeprefix("cx-"): val.strip()
        for raw, val in CSS_DECL_RE.findall(tok_top.group(1))
        if raw.startswith("cx-")
    }
    cx_dark = {
        raw.removeprefix("cx-"): val.strip()
        for raw, val in CSS_DECL_RE.findall(tok_dark.group(1))
        if raw.startswith("cx-")
    }

    style_top = re.search(r"(?:^|\n):root\s*\{([^}]*)\}", style_css_text)
    assert style_top is not None, "missing top-level :root block in src/style.css"
    style_dark = re.search(
        r"@media\s*\(\s*prefers-color-scheme\s*:\s*dark\s*\)\s*\{\s*:root\s*\{([^}]*)\}\s*\}",
        style_css_text,
    )

    app_light: dict[str, str] = {}
    adw_light: dict[str, str] = {}
    for raw_name, value in CSS_DECL_RE.findall(style_top.group(1)):
        assert not raw_name.startswith("cx-"), "src/style.css must import --cx-* from generated src/tokens.css"
        if raw_name.startswith("app-"):
            app_light[raw_name.removeprefix("app-")] = value.strip()
        else:
            adw_light[raw_name] = value.strip()

    app_dark: dict[str, str] = {}
    if style_dark is not None:
        for raw_name, value in CSS_DECL_RE.findall(style_dark.group(1)):
            assert not raw_name.startswith("cx-"), "src/style.css dark :root must not redefine --cx-*"
            if raw_name.startswith("app-"):
                app_dark[raw_name.removeprefix("app-")] = value.strip()

    return cx_light, cx_dark, app_light, app_dark, adw_light


def test_vendored_tokens_sha256_generated_files_and_literal_scan_pass():
    gen = _load_generator()
    gen.check_all(ROOT)
    assert not gen.scan_source_literals(ROOT, allow_pre_w1=False)

    upstream = json.loads(UPSTREAM.read_text(encoding="utf-8"))
    commit = upstream["commit"]
    assert re.fullmatch(r"[0-9a-f]{40}", commit)
    for rel_key, expected_sha in upstream["sha256"].items():
        assert hashlib.sha256((ROOT / "ux/brand" / rel_key).read_bytes()).hexdigest() == expected_sha
    assert {
        "tokens.json",
        "mark/saturn-nav.svg",
        "mark/saturn-mid.svg",
        "mark/saturn-full.svg",
        "mark/mark-flat.svg",
    }.issubset(upstream["sha256"])
    ux_upstream = json.loads((ROOT / "tests/ux/UPSTREAM.json").read_text(encoding="utf-8"))
    assert commit == ux_upstream["commit"]
    assert commit == tokens.UPSTREAM_COMMIT

    for path in (TOKENS_CSS, BRAND_TOKENS_PY):
        head = "\n".join(path.read_text(encoding="utf-8").splitlines()[:4])
        match = HEADER_RE.search(head)
        assert match is not None and match.group(1) == commit, f"{path} header must name {commit}"

    assert '@import url("tokens.css");' in STYLE_CSS.read_text(encoding="utf-8")


def test_generated_tokens_match_all_53_decided_tokens_and_derived():
    payload = json.loads(VENDORED.read_text(encoding="utf-8"))
    assert payload["schema"] == "cassini-ux/brand-tokens@1"
    assert payload["modes"] == ["light", "dark"]
    assert len(payload["tokens"]) == 53
    assert all(spec["status"] == "decided" for spec in payload["tokens"].values())
    assert all("check" not in spec for spec in payload["tokens"].values())
    assert all("check" not in spec for spec in payload["derived"].values())
    assert payload["exceptions"] == []

    cx_light, cx_dark, *_ = parse_css_theme(
        TOKENS_CSS.read_text(encoding="utf-8"),
        STYLE_CSS.read_text(encoding="utf-8"),
    )
    expected_base = {k: (v["light"].upper(), v["dark"].upper()) for k, v in payload["tokens"].items()}
    expected_derived = {k: (v["light"].upper(), v["dark"].upper()) for k, v in payload["derived"].items()}
    assert expected_base == tokens.TOKENS
    assert expected_derived == tokens.DERIVED_TOKENS
    assert {k: (cx_light[k], cx_dark[k]) for k in cx_light} == (expected_base | expected_derived)


def test_generator_check_fails_on_sha256_drift_generated_drift_or_source_literals(tmp_path):
    gen = _load_generator()
    for rel in (
        "ux/brand/UPSTREAM.json",
        "ux/brand/tokens.json",
        "ux/brand/mark/saturn-nav.svg",
        "ux/brand/mark/saturn-mid.svg",
        "ux/brand/mark/saturn-full.svg",
        "ux/brand/mark/mark-flat.svg",
        "src/brand/mark/saturn-nav.svg",
        "src/brand/mark/saturn-mid.svg",
        "src/brand/mark/saturn-full.svg",
        "src/brand/mark/mark-flat.svg",
        "src/tokens.css",
        "src/style.css",
        "src/cassini_talk/_brand_tokens.py",
        "src/cassini_talk/tokens.py",
        "src/cassini_talk/meeting_page.py",
    ):
        dst = tmp_path / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / rel, dst)

    gen.check_all(tmp_path)

    # 1. Drift in generated CSS fails.
    (tmp_path / "src/tokens.css").write_text(
        (tmp_path / "src/tokens.css").read_text(encoding="utf-8").replace("#F4F5F7", "#F4F5F6", 1),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="src/tokens.css differs"):
        gen.check_all(tmp_path)
    shutil.copyfile(TOKENS_CSS, tmp_path / "src/tokens.css")

    # 2. Drift in generated Python fails.
    (tmp_path / "src/cassini_talk/_brand_tokens.py").write_text(
        (tmp_path / "src/cassini_talk/_brand_tokens.py").read_text(encoding="utf-8").replace("#093351", "#093350", 1),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="src/cassini_talk/_brand_tokens.py differs"):
        gen.check_all(tmp_path)
    shutil.copyfile(BRAND_TOKENS_PY, tmp_path / "src/cassini_talk/_brand_tokens.py")

    # 3. Colour literal injected into a Python source file fails (while #230 in docstrings passes).
    (tmp_path / "src/cassini_talk/probe.py").write_text(
        '"""Issue #230 docstring is allowed."""\nBAD = "#1c71d8"\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="colour literal '#1c71d8'"):
        gen.check_all(tmp_path)
    (tmp_path / "src/cassini_talk/probe.py").unlink()

    # 4. Fixed palette / rgb() literal injected into component CSS fails.
    (tmp_path / "src/extra.css").write_text(".bad { color: @red_3; background: rgb(1, 2, 3); }\n", encoding="utf-8")
    with pytest.raises(ValueError, match="colour literal"):
        gen.check_all(tmp_path)
    (tmp_path / "src/extra.css").unlink()

    # 5. Tampered vendored tokens.json fails SHA-256 verification.
    (tmp_path / "ux/brand/tokens.json").write_text(
        (tmp_path / "ux/brand/tokens.json").read_text(encoding="utf-8") + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        gen.check_all(tmp_path)


def test_every_applied_w1_role_resolves_to_its_token_and_unapplied_set_is_exact():
    tokens_css_text = TOKENS_CSS.read_text(encoding="utf-8")
    style_css_text = STYLE_CSS.read_text(encoding="utf-8")
    _cx_light, _cx_dark, app_light, app_dark, adw_light = parse_css_theme(tokens_css_text, style_css_text)

    all_tokens = tokens.TOKENS | tokens.DERIVED_TOKENS
    assert set(app_light) == set(tokens.CSS_ROLE_TOKENS), (
        f"CSS --app-* roles and tokens.CSS_ROLE_TOKENS differ: {set(app_light) ^ set(tokens.CSS_ROLE_TOKENS)}"
    )
    for role, target in tokens.ROLE_TOKENS.items():
        assert target in all_tokens, f"role {role!r} maps to unknown token {target!r}"

    allowed_w1 = set(tokens.ROLE_TOKENS) - tokens.DEFERRED_W2_ROLES
    assert not (tokens.UNAPPLIED_W1_ROLES - allowed_w1)
    assert not tokens.UNAPPLIED_W1_ROLES, f"Wave 1 preview left roles unapplied: {sorted(tokens.UNAPPLIED_W1_ROLES)}"

    for adw_name, target in tokens.ADW_ROLE_TOKENS.items():
        role = f"adw:{adw_name}"
        if role not in tokens.UNAPPLIED_W1_ROLES:
            assert adw_light.get(adw_name) == f"var(--cx-{target})"

    for css_role, target in tokens.CSS_ROLE_TOKENS.items():
        if css_role in tokens.DEFERRED_W2_ROLES or css_role in tokens.UNAPPLIED_W1_ROLES:
            continue
        assert f"var(--cx-{target})" in app_light[css_role], f"--app-{css_role} must read --cx-{target}"
        assert css_role not in app_dark, f"--app-{css_role} must switch via --cx-{target}, not dark override"

    python_checks = {
        "played-word-bg": lambda dark: tokens.played_word_colours(dark=dark)[0],
        "played-word-fg": lambda dark: tokens.played_word_colours(dark=dark)[1],
        "search-hit-bg": lambda dark: tokens.search_hit_colours(dark=dark, selected=False)[0],
        "search-hit-selected-bg": lambda dark: tokens.search_hit_colours(dark=dark, selected=True)[0],
        "search-hit-fg": lambda dark: tokens.search_hit_colours(dark=dark, selected=False)[1],
        "spectro-pill-ink": lambda dark: tokens.spectrogram_colours("pill", dark=dark, silent=False)[0],
        "spectro-row-ink": lambda dark: tokens.spectrogram_colours("row", dark=dark, silent=False)[0],
        "spectro-pill-silent": lambda dark: tokens.spectrogram_colours("pill", dark=dark, silent=True)[1],
        "spectro-row-silent": lambda dark: tokens.spectrogram_colours("row", dark=dark, silent=True)[1],
        "timeline-tick": lambda dark: tokens.timeline_tick_colour(dark=dark),
        "aside-chip-bg": lambda dark: tokens.aside_chip_wash(dark=dark)[0],
    }
    assert tokens.aside_chip_wash(dark=False) == (tokens.token("ink", dark=False), "10%")
    assert tokens.aside_chip_wash(dark=True) == (tokens.token("ink", dark=True), "10%")
    for py_role, getter in python_checks.items():
        if py_role not in tokens.UNAPPLIED_W1_ROLES:
            target = tokens.PYTHON_ROLE_TOKENS[py_role]
            assert getter(False) == tokens.token(target, dark=False)
            assert getter(True) == tokens.token(target, dark=True)

    for slot in range(7):
        speaker = SimpleNamespace(
            colour_index=0 if slot == 0 else slot - 1,
            provenance="unnamed" if slot == 0 else "user",
        )
        assert tokens.speaker_slot(speaker) == slot
        assert tokens.speaker_avatar_wash(speaker, dark=False) == tokens.token(f"avatar-{slot}", dark=False)
        assert tokens.speaker_avatar_wash(speaker, dark=True) == tokens.token(f"avatar-{slot}", dark=True)
        if f"speaker-{slot}" not in tokens.UNAPPLIED_W1_ROLES:
            assert tokens.speaker_colour(speaker, dark=False) == tokens.token(f"speaker-{slot}", dark=False)
            assert tokens.speaker_colour(speaker, dark=True) == tokens.token(f"speaker-{slot}", dark=True)
            assert f".speaker-ink-{slot}" in style_css_text
        if f"avatar-{slot}" not in tokens.UNAPPLIED_W1_ROLES:
            assert f".speaker-avatar-{slot}" in style_css_text
            assert f".speaker-chip-{slot}" in style_css_text
    assert tokens.speaker_slot(SimpleNamespace(colour_index=6, provenance="user")) == 1
    assert tokens.speaker_slot(SimpleNamespace(colour_index=1, stored_colour_index=2, provenance="user")) == 3
    assert tokens.speaker_slot(SimpleNamespace(colour_index=4, stored_colour_index=2, provenance="unnamed")) == 0
    assert [tokens.saved_voice_slot({"saveOrder": i}) for i in range(7)] == [1, 2, 3, 4, 5, 6, 1]


def test_about_dialog_wires_saturn_mid_gresource_at_pinned_sha256():
    import xml.etree.ElementTree as ET

    upstream = json.loads(UPSTREAM.read_text(encoding="utf-8"))
    mark_upstream = json.loads((ROOT / "ux/brand/mark/UPSTREAM.json").read_text(encoding="utf-8"))
    expected_sha = upstream["sha256"]["mark/saturn-mid.svg"]
    assert expected_sha == mark_upstream["files"]["saturn-mid.svg"]["sha256"]
    assert hashlib.sha256((ROOT / "src/brand/mark/saturn-mid.svg").read_bytes()).hexdigest() == expected_sha

    gresource_tree = ET.parse(ROOT / "src/cassini-talk.gresource.xml")
    files = {
        (el.attrib.get("alias") or (el.text or "").strip()): (el.text or "").strip()
        for el in gresource_tree.findall(".//file")
    }
    assert files.get("brand/mark/saturn-mid.svg") == "brand/mark/saturn-mid.svg"
    assert files.get("icons/scalable/apps/saturn-mid.svg") == "brand/mark/saturn-mid.svg"

    main_py = (ROOT / "src/cassini_talk/main.py").read_text(encoding="utf-8")
    on_about_block = main_py.split("def on_about(", 1)[1].split("\ndef ", 1)[0]
    assert "application_icon=ensure_about_mark_icon()" in on_about_block
    assert "self.get_application_id()" not in on_about_block


def test_brand_mark_dimensions_resource_thresholds_and_wordmark(tmp_path):
    import subprocess
    from unittest.mock import MagicMock

    gi = pytest.importorskip("gi")
    try:
        gi.require_version("Gtk", "4.0")
    except ValueError:
        pytest.skip("GTK 4 introspection is unavailable")
    from gi.repository import Gio, Gtk

    from cassini_talk import brand_mark

    assert brand_mark.WORDMARK_TEXT == "cassini.talk"
    assert brand_mark.WORDMARK_FONT_PX == 18
    assert brand_mark.WORDMARK_FONT_WEIGHT == 500
    assert brand_mark.ABOUT_MARK_ICON_NAME == "saturn-mid"
    assert brand_mark.ABOUT_MARK_ICON_RESOURCE == "/io/codemyriad/CassiniTalk/icons/scalable/apps/saturn-mid.svg"
    assert brand_mark.mark_dimensions(26) == (40, 26)
    assert brand_mark.mark_dimensions(28) == (43, 28)
    assert brand_mark.mark_dimensions(56) == (86, 56)
    assert brand_mark.mark_resource_for_height(26) == brand_mark.NAV_MARK_RESOURCE
    assert brand_mark.mark_resource_for_height(40) == brand_mark.NAV_MARK_RESOURCE
    assert brand_mark.mark_resource_for_height(41) == brand_mark.MID_MARK_RESOURCE

    fake_theme = MagicMock()
    fake_theme.get_resource_path.return_value = []
    fake_display = object()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(Gtk.IconTheme, "get_for_display", lambda d: fake_theme if d is fake_display else None)
        assert brand_mark.ensure_about_mark_icon(fake_display) == "saturn-mid"
    added = [call.args[0] for call in fake_theme.add_resource_path.call_args_list]
    assert added == [brand_mark.ICON_RESOURCE_PREFIX, brand_mark.RESOURCE_PREFIX]

    compiler = shutil.which("glib-compile-resources")
    if compiler is not None:
        bundle = tmp_path / "cassini-talk.gresource"
        src_dir = ROOT / "src"
        subprocess.run(
            [compiler, str(src_dir / "cassini-talk.gresource.xml"), f"--sourcedir={src_dir}", f"--target={bundle}"],
            check=True,
        )
        res = Gio.Resource.load(str(bundle))
        Gio.resources_register(res)
        try:
            expected_sha = json.loads(UPSTREAM.read_text(encoding="utf-8"))["sha256"]["mark/saturn-mid.svg"]
            for res_path in (brand_mark.MID_MARK_RESOURCE, brand_mark.ABOUT_MARK_ICON_RESOURCE):
                raw = Gio.resources_lookup_data(res_path, Gio.ResourceLookupFlags.NONE).get_data()
                assert hashlib.sha256(raw).hexdigest() == expected_sha
        finally:
            Gio.resources_unregister(res)
