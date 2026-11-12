#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Validate and render user-facing release notes for stable and daily test builds."""

from __future__ import annotations

import argparse
import re
from collections.abc import Sequence
from pathlib import Path

KINDS = ("new", "better", "fixed")
GROUP_TITLES = {"new": "New", "better": "Better", "fixed": "Fixed"}
USER_NOTES_LIMIT = 500
EMPTY_USER_NOTES = "Fixes and improvements under the hood. Nothing changes in how you use Cassini."
OVERFLOW_SUFFIX = "and smaller fixes"
SLUG_RE = re.compile(r"[a-z0-9][a-z0-9-]*\.md")
FORBIDDEN_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("placeholder text", re.compile(r"(?i)\b(?:not released yet|todo|tbd)\b")),
    ("pull request number", re.compile(r"#\d+")),
    ("decision identifier", re.compile(r"\bD\d+(?:\.\d+)?\b")),
    ("CI reference", re.compile(r"\bCI\b")),
    ("PR reference", re.compile(r"\bPRs?\b")),
    ("'please'", re.compile(r"(?i)\bplease\b")),
    ("source file name", re.compile(r"\b[\w/-]+\.(?:py|yml|yaml|ui|css|sh|json|rs|kt|swift)\b")),
)


def validate_user_text(
    text: str, *, source: str = "release notes", require_typographic_apostrophe: bool = True
) -> None:
    if not text or not text.strip():
        raise ValueError(f"{source} must not be empty")
    for label, pattern in FORBIDDEN_PATTERNS:
        if match := pattern.search(text):
            raise ValueError(f"{source} must not contain {label} ({match[0]!r})")
    if require_typographic_apostrophe and "'" in text:
        raise ValueError(f"{source} must use typographic apostrophes (’), not ASCII '''")


def parse_fragment(raw: str, *, name: str = "<fragment>") -> tuple[str, str]:
    filename = Path(name).name
    if filename != "<fragment>" and not SLUG_RE.fullmatch(filename):
        raise ValueError(f"Invalid release-note fragment filename {filename!r} (expected <short-slug>.md)")
    header, sep, body = raw.partition("\n---\n")
    if not sep:
        raise ValueError(f"{name}: missing '---' separator after kind header")
    header_lines = [line.strip() for line in header.strip().splitlines() if line.strip()]
    if len(header_lines) != 1 or not header_lines[0].startswith("kind:"):
        raise ValueError(f"{name}: expected single 'kind: new | better | fixed' header")
    kind = header_lines[0].removeprefix("kind:").strip()
    if kind not in KINDS:
        raise ValueError(f"{name}: invalid kind {kind!r} (expected new, better, or fixed)")
    sentence = " ".join(body.strip().split())
    validate_user_text(sentence, source=name)
    return kind, sentence


def load_fragments_dir(directory: Path) -> list[tuple[str, str]]:
    if not directory.is_dir():
        return []
    entries = sorted(path for path in directory.iterdir() if path.is_file() and not path.name.startswith("."))
    return [parse_fragment(path.read_text(encoding="utf-8"), name=path.as_posix()) for path in entries]


def _format_grouped(items: Sequence[tuple[str, str]]) -> str:
    blocks: list[str] = []
    for kind in KINDS:
        bullets = [f"- {text}" for item_kind, text in items if item_kind == kind]
        if bullets:
            blocks.append(f"{GROUP_TITLES[kind]}\n" + "\n".join(bullets))
    return "\n\n".join(blocks)


def _shorten_sentence(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    if max_len <= 1:
        return "…"[:max_len]
    cut = text[: max_len - 1].rstrip()
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0].rstrip(" ,;:-")
    return f"{cut}…"


def render_user_notes(fragments: Sequence[tuple[str, str]], *, limit: int = USER_NOTES_LIMIT) -> str:
    if limit < len(EMPTY_USER_NOTES):
        raise ValueError(f"limit ({limit}) is smaller than fallback user notes")
    if not fragments:
        return EMPTY_USER_NOTES
    for kind, sentence in fragments:
        if kind not in KINDS:
            raise ValueError(f"Invalid fragment kind: {kind!r}")
        validate_user_text(sentence)

    ordered = [(kind, sentence) for target in KINDS for kind, sentence in fragments if kind == target]
    full = _format_grouped(ordered)
    if len(full) <= limit:
        return full

    suffix = f"\n\n{OVERFLOW_SUFFIX}"
    kept: list[tuple[str, str]] = []
    for item in ordered:
        candidate = _format_grouped([*kept, item]) + suffix
        if len(candidate) <= limit:
            kept.append(item)
        else:
            break

    if kept:
        rendered = _format_grouped(kept) + suffix
    else:
        first_kind, first_sentence = ordered[0]
        prefix = f"{GROUP_TITLES[first_kind]}\n- "
        available = limit - len(prefix) - len(suffix)
        rendered = f"{prefix}{_shorten_sentence(first_sentence, available)}{suffix}"

    validate_user_text(rendered)
    if len(rendered) > limit:
        raise AssertionError(f"Rendered user notes exceeded {limit} chars ({len(rendered)})")
    return rendered


def build_prerelease_body(user_notes: str, developer_changelog: str) -> str:
    validate_user_text(user_notes, source="pre-release user notes")
    if len(user_notes) > USER_NOTES_LIMIT:
        raise ValueError(f"User release notes exceed {USER_NOTES_LIMIT} characters ({len(user_notes)})")
    dev = developer_changelog.strip()
    if not dev:
        raise ValueError("Developer changelog must not be empty")
    return f"{user_notes}\n\n<details>\n<summary>Developer changelog</summary>\n\n{dev}\n</details>\n"


def notes(news: str, version: str) -> str:
    """The curated NEWS section for stable `version`, refusing missing or developer-only text."""
    sections = re.split(r"^Version (\d+\.\d+\.\d+)\n~+\n", news, flags=re.MULTILINE)
    for name, body in zip(sections[1::2], sections[2::2], strict=True):
        if name == version:
            body = re.sub(r"\AReleased: [^\n]*\n", "", body).strip()
            if body:
                validate_user_text(body, source=f"NEWS {version}")
                return body + "\n"
    raise ValueError(f"NEWS has no notes for version {version}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version", help="X.Y.Z, or the tag vX.Y.Z")
    parser.add_argument("--news", type=Path, default=Path(__file__).resolve().parents[2] / "NEWS")
    args = parser.parse_args()
    try:
        print(notes(args.news.read_text(encoding="utf-8"), args.version.removeprefix("v")), end="")
    except (OSError, ValueError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
