# Copyright 2026 Codemyriad OÜ
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for the W0.8 screenshot manifest and harness unpinning."""

import importlib.util
import json
import struct
import zlib
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location("ux_shots_manifest", ROOT / "build-aux/ci/ux_shots_manifest.py")
manifest_mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(manifest_mod)


def _tiny_png(width: int = 1120, height: int = 720) -> bytes:
    def chunk(tag: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", len(payload)) + tag + payload + struct.pack(">I", zlib.crc32(tag + payload))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    idat = zlib.compress(b"\x00\x00\x00\x00\xff")
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


def test_write_manifest_normalizes_dark_conventions_locales_and_scales(tmp_path):
    files = (
        ("library.png", (1120, 720)),
        ("dark/library.png", (1120, 720)),
        ("search.png", (1120, 720)),
        ("dark/search.png", (1120, 720)),
        ("first-run-fr.png", (1120, 720)),
        ("dark/first-run-fr.png", (1120, 720)),
        ("first-run-pt-BR.png", (1120, 720)),
        ("dark/first-run-pt-BR.png", (1120, 720)),
        ("what-is-recorded-browser.png", (1120, 720)),
        ("what-is-recorded-browser-dark.png", (1120, 720)),
        ("companion-health-light.png", (1120, 720)),
        ("companion-health-dark.png", (1120, 720)),
        ("spectrogram-components-2x.png", (2240, 1440)),
        ("dark/spectrogram-components-2x.png", (2240, 1440)),
        ("spectrogram-dropout.png", (1120, 720)),
        ("spectrogram-dropout-light.png", (1120, 720)),
        ("spectrogram-dropout-dark.png", (1120, 720)),
    )
    for rel, (w, h) in files:
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(_tiny_png(w, h))

    out = manifest_mod.write_manifest(tmp_path)
    data = json.loads(out.read_text())
    assert data["app"] == "gnome" and data["version"] == 1
    rows = data["shots"]
    assert len(rows) == len(files)
    assert [rows[0]["key"], rows[1]["key"]] == ["library.light", "library.dark"]
    by_key = {row["key"]: row for row in rows}
    assert by_key["library.light"]["board"] == "Main"
    assert by_key["search.dark"]["path"] == "dark/search.png"
    assert by_key["what-is-recorded-browser.dark"]["board"] == "WhatIsRecorded"
    assert by_key["what-is-recorded-browser.dark"]["source"] == "tests/ui/what-is-recorded.py"
    assert by_key["first-run.dark.pt-br"]["lang"] == "pt-br"
    assert by_key["spectrogram-components-2x.dark"]["scale"] == 2
    assert by_key["spectrogram-components-2x.dark"]["width"] == 2240
    assert by_key["spectrogram-dropout-capture.light"]["path"] == "spectrogram-dropout.png"


def test_manifest_rejects_corrupt_png_and_unknown_source(tmp_path):
    (tmp_path / "library.png").write_bytes(b"not-a-png")
    with pytest.raises(ValueError, match="Invalid PNG header"):
        manifest_mod.build_manifest(tmp_path)
    (tmp_path / "library.png").unlink()
    (tmp_path / "mystery-screen.png").write_bytes(_tiny_png())
    with pytest.raises(ValueError, match="Unknown screenshot source"):
        manifest_mod.build_manifest(tmp_path)


def test_accent_unpinned_and_ux_shots_script_wired():
    wrapper = (ROOT / "build-aux/ux-screenshot.sh").read_text()
    workflow = (ROOT / ".github/workflows/ux-shot.yml").read_text()
    shots_sh = (ROOT / "build-aux/ux-shots.sh").read_text()
    assert "ADW_DEBUG_ACCENT_COLOR=blue" not in wrapper
    assert "unset " in wrapper and "ADW_DEBUG_ACCENT_COLOR" in wrapper
    assert "accent']==" not in workflow and 'accent"]==' not in workflow
    assert 'build-aux/ux-shots.sh "$BOARD_APP" "$CASSINI_BOARD_BUILD" ux-artifacts' in workflow
    assert 'python3 build-aux/ci/ux_shots_manifest.py "$out"' in shots_sh
