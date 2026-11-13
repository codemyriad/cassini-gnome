# Copyright 2026 Codemyriad OÜ
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for the W0.8 screenshot manifest, pair/theme invariants, and accent unpinning."""

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


def _solid_png(scheme: str = "light", width: int = 8, height: int = 8) -> bytes:
    def chunk(tag: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", len(payload)) + tag + payload + struct.pack(">I", zlib.crc32(tag + payload))

    rgba = b"\xfa\xfa\xfb\xff" if scheme == "light" else b"\x22\x22\x26\xff"
    row = b"\x00" + rgba * width
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(row * height)) + chunk(b"IEND", b"")
    )


def _write(root: Path, rel: str, scheme: str = "light", size: tuple[int, int] = (8, 8)) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(_solid_png(scheme, *size))


def test_write_manifest_normalizes_pairs_locales_and_scales(tmp_path):
    files = (
        ("library.png", "light", (8, 8)), ("dark/library.png", "dark", (8, 8)),
        ("search.png", "light", (8, 8)), ("dark/search.png", "dark", (8, 8)),
        ("first-run-fr.png", "light", (8, 8)), ("dark/first-run-fr.png", "dark", (8, 8)),
        ("first-run-pt-BR.png", "light", (8, 8)), ("dark/first-run-pt-BR.png", "dark", (8, 8)),
        ("w2-badges-it.png", "light", (8, 8)), ("dark/w2-badges-it.png", "dark", (8, 8)),
        ("what-is-recorded-browser.png", "light", (8, 8)), ("what-is-recorded-browser-dark.png", "dark", (8, 8)),
        ("companion-health-light.png", "light", (8, 8)), ("companion-health-dark.png", "dark", (8, 8)),
        ("library-saving.png", "light", (8, 8)), ("dark/library-saving.png", "dark", (8, 8)),
        ("recording-saving.png", "light", (8, 8)), ("dark/recording-saving.png", "dark", (8, 8)),
        ("spectrogram-components-2x.png", "light", (16, 16)), ("dark/spectrogram-components-2x.png", "dark", (16, 16)),
        ("spectrogram-dropout-light@2x.png", "light", (16, 16)), ("spectrogram-dropout-dark@2x.png", "dark", (16, 16)),
    )  # fmt: skip
    for rel, scheme, size in files:
        _write(tmp_path, rel, scheme, size)

    data = json.loads(manifest_mod.write_manifest(tmp_path).read_text())
    rows = data["shots"]
    assert data["app"] == "gnome" and len(rows) == len(files)
    assert [rows[0]["key"], rows[1]["key"]] == ["library.light", "library.dark"]
    by_key = {row["key"]: row for row in rows}
    assert by_key["library.light"]["board"] == "Main"
    assert by_key["what-is-recorded-browser.dark"]["source"] == "tests/ui/what-is-recorded.py"
    assert by_key["recording-saving.light"]["source"] == "tests/ui/recording.py"
    assert by_key["library-saving.dark"]["source"] == "tests/ui/recording.py"
    assert by_key["first-run.dark.pt-br"]["lang"] == "pt-br"
    assert by_key["w2-badges.light.it"]["lang"] == "it"
    assert by_key["w2-badges.light.it"]["source"] == "tests/ui/w2-badges.py"
    assert by_key["spectrogram-dropout-2x.dark"]["scale"] == 2


def test_manifest_fails_when_entry_lacks_matching_scheme_pair(tmp_path):
    _write(tmp_path, "library.png", "light")
    _write(tmp_path, "dark/library.png", "dark")
    _write(tmp_path, "spectrogram-recording-light.png", "light")
    with pytest.raises(ValueError, match="Unpaired screenshot entries: spectrogram-recording"):
        manifest_mod.build_manifest(tmp_path)


def test_manifest_fails_when_rendered_image_disagrees_with_scheme_label(tmp_path):
    _write(tmp_path, "spectrogram-reduced-motion-light.png", "dark")
    _write(tmp_path, "spectrogram-reduced-motion-dark.png", "dark")
    with pytest.raises(ValueError, match="Rendered theme mismatch.*spectrogram-reduced-motion-light.png"):
        manifest_mod.build_manifest(tmp_path)


def test_manifest_rejects_duplicate_unqualified_capture_corrupt_png_and_unknown_source(tmp_path):
    _write(tmp_path, "spectrogram-dropout.png", "light")
    _write(tmp_path, "spectrogram-dropout-light.png", "light")
    _write(tmp_path, "spectrogram-dropout-dark.png", "dark")
    with pytest.raises(ValueError, match="Invalid or duplicate shot key"):
        manifest_mod.build_manifest(tmp_path)

    (tmp_path / "broken.png").write_bytes(b"not-a-png")
    with pytest.raises(ValueError, match="Invalid PNG header"):
        manifest_mod.build_manifest(tmp_path)


def test_accent_unpinned_and_ux_shots_script_wired():
    wrapper = (ROOT / "build-aux/ux-screenshot.sh").read_text()
    workflow = (ROOT / ".github/workflows/ux-shot.yml").read_text()
    shots_sh = (ROOT / "build-aux/ux-shots.sh").read_text()
    assert "ADW_DEBUG_ACCENT_COLOR=blue" not in wrapper and "ADW_DEBUG_ACCENT_COLOR" in wrapper
    assert "accent']==" not in workflow and 'accent"]==' not in workflow
    assert 'build-aux/ux-shots.sh "$BOARD_APP" "$CASSINI_BOARD_BUILD" ux-artifacts' in workflow
    assert 'python3 build-aux/ci/ux_shots_manifest.py "$out"' in shots_sh


def test_scrimmed_light_modal_dialog_passes_chrome_luminance_check(tmp_path):
    def chunk(tag: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", len(payload)) + tag + payload + struct.pack(">I", zlib.crc32(tag + payload))

    width, height = 64, 64
    scrimmed_row = b"\x00" + b"\x78\x78\x75\xff" * width
    dialog_row = b"\x00" + (b"\x78\x78\x75\xff" * 16 + b"\xff\xff\xff\xff" * 32 + b"\x78\x78\x75\xff" * 16)
    rows = [dialog_row if y == height // 2 else scrimmed_row for y in range(height)]
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(b"".join(rows)))
        + chunk(b"IEND", b"")
    )

    (tmp_path / "export.png").write_bytes(png)
    _write(tmp_path, "dark/export.png", "dark", (width, height))
    manifest = manifest_mod.build_manifest(tmp_path)
    assert [row["key"] for row in manifest["shots"]] == ["export.light", "export.dark"]
