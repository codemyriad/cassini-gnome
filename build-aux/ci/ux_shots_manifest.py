#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Write and verify a deterministic shots.json manifest for ux-artifacts."""

import json
import re
import struct
import sys
import zlib
from pathlib import Path

PNG_SIG = b"\x89PNG\r\n\x1a\n"
KEY_RE = re.compile(r"^[a-z0-9][a-z0-9.-]*$")
CORE_ORDER = (
    "library", "first-run", "recording", "record-first", "meeting", "received",
    "search", "export", "settings-recording", "settings-transcription", "speakers", "saved-voices",
)  # fmt: skip
BOARDS = frozenset(CORE_ORDER)
GALLERY_BOARDS = {
    "library": "Main", "first-run": "LibraryEmpty", "received": "LibraryReceived", "search": "Search",
    "record-first": "RecordFirst", "recording": "Recording", "what-is-recorded-browser": "WhatIsRecorded",
    "background": "RecordHidden", "meeting": "Meeting", "speakers": "Speakers", "export": "Export",
    "settings-recording": "Settings", "settings-transcription": "Settings", "saved-voices": "SavedVoices",
    "companion-setup-pending": "CompanionSetup", "trash-undo": "Trash",
}  # fmt: skip
FLOW_SOURCES = (
    ("background", "tests/ui/background.py"), ("batch-import-gui", "tests/ui/batch-import.py"),
    ("companion-health", "tests/ui/companion-health.py"), ("companion-setup-", "tests/ui/companion.py"),
    ("import-export-gui", "tests/ui/import-export.py"), ("interjections-", "tests/ui/interjections.py"),
    ("journey-parity", "tests/ui/journey-parity.py"), ("library-saving", "tests/ui/recording.py"),
    ("listening-rows", "tests/ui/listening-rows.py"), ("meeting-gui-", "tests/ui/meeting.py"),
    ("recording-details-", "tests/ui/recording-details.py"), ("recording-gui", "tests/ui/recording.py"),
    ("recording-saving", "tests/ui/recording.py"),
    ("rename", "tests/ui/rename.py"), ("spectrogram-browser", "tests/ui/spectrogram-browser.py"),
    ("spectrogram-components", "tests/ui/spectrogram.py"), ("spectrogram-", "tests/ui/spectrogram-live.py"),
    ("speech-models-", "tests/ui/speech-models.py"), ("starring", "tests/ui/starring.py"), ("test-recording-", "tests/ui/test-recording.py"),
    ("trash-", "tests/ui/trash.py"), ("w1-", "tests/ui/w1-token-states.py"),
    ("w2-badges", "tests/ui/w2-badges.py"), ("what-is-recorded-", "tests/ui/what-is-recorded.py"),
    ("wide-speakers", "tests/ui/wide-speakers.py"),
)  # fmt: skip


def _source_for(base_screen: str, lang: str) -> str:
    if base_screen in BOARDS:
        return "build-aux/ux-screenshot.sh" if lang != "en" else "build-aux/ux-boards.sh"
    for prefix, script in FLOW_SOURCES:
        if base_screen == prefix or base_screen.startswith(prefix):
            return script
    raise ValueError(f"Unknown screenshot source for {base_screen!r}")


def _chrome_luminance(data: bytes) -> tuple[int, int, float]:
    if len(data) < 24 or data[:8] != PNG_SIG:
        raise ValueError("Invalid PNG header")
    pos, width, height, color_type, idat = 8, 0, 0, 0, bytearray()
    while pos < len(data):
        length, tag = struct.unpack(">I4s", data[pos : pos + 8])
        payload = data[pos + 8 : pos + 8 + length]
        pos += 12 + length
        if tag == b"IHDR":
            width, height, depth, color_type, _, _, interlace = struct.unpack(">IIBBBBB", payload)
            if depth != 8 or color_type not in (2, 6) or interlace != 0:
                raise ValueError("Unsupported PNG format")
        elif tag == b"IDAT":
            idat.extend(payload)
        elif tag == b"IEND":
            break
    bpp = 4 if color_type == 6 else 3
    stride, raw = width * bpp, zlib.decompress(bytes(idat))
    top_y = min(height // 2, 48 if width >= 2000 else 24)
    mid_y = height // 2
    targets = {top_y, mid_y}
    step = max(1, width // 32)
    xs = range(0, width, step) if width < 32 else range(step, width - step, step)
    row_lums: list[float] = []
    prev, cur, offset = bytearray(stride), bytearray(stride), 0
    for y in range(max(targets) + 1):
        flt, line = raw[offset], raw[offset + 1 : offset + 1 + stride]
        offset += 1 + stride
        for i in range(stride):
            a = cur[i - bpp] if i >= bpp else 0
            b, c = prev[i], (prev[i - bpp] if i >= bpp else 0)
            if flt == 0:
                cur[i] = line[i]
            elif flt == 1:
                cur[i] = (line[i] + a) & 0xFF
            elif flt == 2:
                cur[i] = (line[i] + b) & 0xFF
            elif flt == 3:
                cur[i] = (line[i] + ((a + b) >> 1)) & 0xFF
            elif flt == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                cur[i] = (line[i] + (a if pa <= pb and pa <= pc else (b if pb <= pc else c))) & 0xFF
            else:
                raise ValueError(f"Unsupported PNG filter {flt}")
        if y in targets:
            samples = [
                (cur[x * bpp] + cur[x * bpp + 1] + cur[x * bpp + 2]) / 3.0
                for x in xs
                if bpp == 3 or cur[x * bpp + 3] >= 200
            ]
            if samples:
                row_lums.append(sum(samples) / len(samples))
        if y < max(targets):
            prev[:] = cur
    if not row_lums:
        raise ValueError("No opaque pixels on sampled chrome scanline")
    return width, height, max(row_lums)


def build_manifest(out_dir: Path) -> dict:
    rels = sorted(p.relative_to(out_dir).as_posix() for p in out_dir.rglob("*.png"))
    rows, seen_keys, groups = [], set(), {}
    core_rank = {name: index for index, name in enumerate(CORE_ORDER)}
    for rel in rels:
        try:
            width, height, lum = _chrome_luminance((out_dir / rel).read_bytes())
        except ValueError as exc:
            raise ValueError(f"{exc}: {rel}") from exc
        in_dark = rel.startswith("dark/")
        stem = (rel[5:] if in_dark else rel)[:-4]
        scale = 1
        if stem.endswith(("@2x", "-2x")):
            scale, stem = 2, stem[:-3]
        elif "-2x-" in stem:
            scale, stem = 2, stem.replace("-2x-", "-", 1)
        lang = "en"
        for suffix, code in (("-pt-BR", "pt-br"), ("-fr", "fr"), ("-it", "it")):
            if stem.endswith(suffix):
                lang, stem = code, stem[: -len(suffix)]
                break
        if in_dark:
            if stem.endswith(("-dark", "-light")):
                raise ValueError(f"Redundant scheme suffix inside dark/: {rel}")
            scheme = "dark"
        elif stem.endswith("-dark"):
            stem, scheme = stem[:-5], "dark"
        elif stem.endswith("-light"):
            stem, scheme = stem[:-6], "light"
        else:
            scheme = "light"
        if (scheme == "dark") != (lum < 128.0):
            raise ValueError(f"Rendered theme mismatch for {rel}: declared {scheme}, luminance {lum:.1f}")
        base_screen = stem.lower()
        screen = f"{base_screen}-2x" if scale == 2 else base_screen
        key = f"{screen}.{scheme}" + (f".{lang}" if lang != "en" else "")
        if not KEY_RE.fullmatch(key) or key in seen_keys:
            raise ValueError(f"Invalid or duplicate shot key {key!r} for {rel}")
        seen_keys.add(key)
        groups.setdefault((screen, lang, "desktop", scale), set()).add(scheme)
        label = screen.replace("-", " ").capitalize()
        rows.append(
            dict(
                key=key,
                screen=screen,
                label=label,
                board=GALLERY_BOARDS.get(base_screen) if scale == 1 else None,
                scheme=scheme,
                lang=lang,
                device="desktop",
                scale=scale,
                width=width,
                height=height,
                path=rel,
                source=_source_for(base_screen, lang),
            )
        )
    unpaired = sorted(f"{k[0]} ({k[1]})" for k, modes in groups.items() if modes != {"light", "dark"})
    if unpaired:
        raise ValueError(f"Unpaired screenshot entries: {', '.join(unpaired)}")
    rows.sort(
        key=lambda r: (
            core_rank.get(r["screen"], len(CORE_ORDER)),
            r["screen"],
            r["lang"],
            r["scale"],
            r["scheme"] != "light",
        )
    )
    return {"app": "gnome", "version": 1, "shots": rows}


def write_manifest(out_dir: Path) -> Path:
    target = out_dir / "shots.json"
    target.write_text(json.dumps(build_manifest(out_dir), indent=2) + "\n")
    return target


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: ux_shots_manifest.py OUTPUT_DIR")
    print(f"Wrote {write_manifest(Path(sys.argv[1]))}")
