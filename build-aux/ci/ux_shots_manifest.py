#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Write a deterministic shots.json manifest for a ux-artifacts directory."""

import json
import re
import struct
import sys
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
    ("journey-parity", "tests/ui/journey-parity.py"), ("listening-rows", "tests/ui/listening-rows.py"),
    ("recording-details-", "tests/ui/recording-details.py"), ("recording-gui", "tests/ui/recording.py"),
    ("rename", "tests/ui/rename.py"), ("spectrogram-browser", "tests/ui/spectrogram-browser.py"),
    ("spectrogram-components", "tests/ui/spectrogram.py"), ("spectrogram-", "tests/ui/spectrogram-live.py"),
    ("speech-models-", "tests/ui/speech-models.py"), ("test-recording-", "tests/ui/test-recording.py"),
    ("trash-", "tests/ui/trash.py"), ("what-is-recorded-", "tests/ui/what-is-recorded.py"),
    ("wide-speakers", "tests/ui/wide-speakers.py"),
)  # fmt: skip


def _source_for(base_screen: str, lang: str) -> str:
    if base_screen in BOARDS:
        return "build-aux/ux-screenshot.sh" if lang != "en" else "build-aux/ux-boards.sh"
    for prefix, script in FLOW_SOURCES:
        if base_screen == prefix or base_screen.startswith(prefix):
            return script
    raise ValueError(f"Unknown screenshot source for {base_screen!r}")


def _label_for(screen: str) -> str:
    text = screen.removesuffix("-2x").replace("-", " ")
    return text[:1].upper() + text[1:]


def build_manifest(out_dir: Path) -> dict:
    rels = sorted(p.relative_to(out_dir).as_posix() for p in out_dir.rglob("*.png"))
    rel_set, rows, seen_keys = set(rels), [], set()
    core_rank = {name: index for index, name in enumerate(CORE_ORDER)}
    for rel in rels:
        data = (out_dir / rel).read_bytes()
        if len(data) < 24 or data[:8] != PNG_SIG:
            raise ValueError(f"Invalid PNG header: {rel}")
        width, height = struct.unpack(">II", data[16:24])
        in_dark = rel.startswith("dark/")
        stem = (rel[5:] if in_dark else rel)[:-4]
        scale = 1
        if stem.endswith(("@2x", "-2x")):
            scale, stem = 2, stem[:-3]
        elif "-2x-" in stem:
            scale, stem = 2, stem.replace("-2x-", "-", 1)
        lang = "en"
        for suffix, code in (("-pt-BR", "pt-br"), ("-fr", "fr")):
            if stem.endswith(suffix):
                lang, stem = code, stem[: -len(suffix)]
                break
        scheme = "dark" if in_dark else "light"
        if not in_dark:
            at2x = "@2x" if rel.endswith("@2x.png") else ""
            if stem.endswith("-dark"):
                stem, scheme = (
                    (f"{stem}-capture", "dark") if f"{stem}-light{at2x}.png" in rel_set else (stem[:-5], "dark")
                )
            elif stem.endswith("-light"):
                stem, scheme = stem[:-6], "light"
            elif f"{stem}-light{at2x}.png" in rel_set and f"{stem}-dark{at2x}.png" in rel_set:
                stem, scheme = f"{stem}-capture", "light"
        base_screen = stem.lower()
        screen = f"{base_screen}-2x" if scale == 2 else base_screen
        key = f"{screen}.{scheme}" + (f".{lang}" if lang != "en" else "")
        if not KEY_RE.fullmatch(key) or key in seen_keys:
            raise ValueError(f"Invalid or duplicate shot key {key!r} for {rel}")
        seen_keys.add(key)
        rows.append(
            dict(
                key=key,
                screen=screen,
                label=_label_for(screen),
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
