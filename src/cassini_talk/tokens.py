# SPDX-License-Identifier: GPL-3.0-or-later
"""App-wide semantic colour role mapping backed by generated _brand_tokens.py (Wave 1)."""

import re

from ._brand_tokens import DERIVED_TOKENS, TOKENS, UPSTREAM_COMMIT
from .strings import text

# Libadwaita 1.8 :root palette variables mapped to their target --cx-* token.
ADW_ROLE_TOKENS: dict[str, str] = {
    "window-bg-color": "ground",
    "window-fg-color": "ink",
    "view-bg-color": "surface",
    "view-fg-color": "ink",
    "headerbar-bg-color": "ground",
    "headerbar-fg-color": "ink",
    "headerbar-backdrop-color": "ground",
    "sidebar-bg-color": "sidebar",
    "sidebar-fg-color": "ink",
    "sidebar-backdrop-color": "sidebar",
    "sidebar-border-color": "line",
    "sidebar-shade-color": "shadow",
    "secondary-sidebar-bg-color": "sidebar",
    "secondary-sidebar-fg-color": "ink",
    "secondary-sidebar-backdrop-color": "sidebar",
    "secondary-sidebar-border-color": "line",
    "secondary-sidebar-shade-color": "shadow",
    "card-bg-color": "surface",
    "card-fg-color": "ink",
    "dialog-bg-color": "raised",
    "dialog-fg-color": "ink",
    "popover-bg-color": "raised",
    "popover-fg-color": "ink",
    "shade-color": "shadow",
    "card-shade-color": "shadow",
    "popover-shade-color": "shadow",
    "accent-bg-color": "accent-strong",
    "accent-fg-color": "on-accent",
    "accent-color": "accent-ink",
    "destructive-bg-color": "error-fill",
    "destructive-fg-color": "on-record",
    "destructive-color": "error",
    "error-bg-color": "error-fill",
    "error-fg-color": "on-record",
    "error-color": "error",
    "success-bg-color": "success-fill",
    "success-fg-color": "on-record",
    "success-color": "success",
    "warning-bg-color": "warn-tint",
    "warning-fg-color": "warn-ink",
    "warning-color": "warn-ink",
}

# Every CSS --app-* role in src/style.css mapped to its target --cx-* token.
CSS_ROLE_TOKENS: dict[str, str] = {
    "window-bg": "ground",
    "window-fg": "ink",
    "view-bg": "surface",
    "view-fg": "ink",
    "dim-fg": "ink-2",
    "placeholder-fg": "ink-3",
    "sidebar-bg": "sidebar",
    "sidebar-fg": "ink",
    "field-bg": "field",
    "field-border": "field-line",
    "headerbar-bg": "ground",
    "hairline": "line-soft",
    "border": "line",
    "export-border": "line",
    "details-border": "line",
    "auto-tag-border": "line",
    "wash-subtle": "neutral-tint",
    "wash-chip": "neutral-tint",
    "wash-trough": "line-soft",
    "row-selected-bg": "surface",
    "search-selected-bg": "surface",
    "primary-bg": "accent-strong",
    "primary-fg": "on-accent",
    "accent-bg": "accent",
    "accent-ink": "accent-ink",
    "accent-tint": "accent-tint",
    "accent-wash": "accent-tint",
    "avatar-wash": "avatar-0",
    "model-selected-bg": "accent-tint",
    "record-bg": "record",
    "record-fg": "on-record",
    "record-ink": "record-ink",
    "record-dot": "record-ink",
    "record-tint": "record-tint",
    "record-halo": "record-tint",
    "record-trough": "record-tint",
    "badge-red-bg": "record-tint",
    "badge-red-fg": "record-ink",
    "badge-transcribing-bg": "ink",
    "badge-transcribing-fg": "ink-2",
    "badge-transcribing-spinner": "accent",
    "badge-amber-bg": "warn-tint",
    "badge-amber-fg": "warn-ink",
    "badge-grey-bg": "ink",
    "badge-grey-fg": "ink-2",
    "badge-received-fg": "ink-2",
    "badge-received-ring": "line",
    "secondary-btn-bg": "ink",
    "error-line-bg": "error-tint",
    "error-line-fg": "error",
    "warn-line-bg": "warn-tint",
    "warn-line-fg": "warn-ink",
    "mic-silent-bg": "warn-tint",
    "mic-silent-fg": "warn-ink",
    "mic-silent-btn-bg": "surface",
    "mic-silent-btn-fg": "warn-ink",
    "mic-silent-trough": "warn-tint",
    "live-silent-trough": "warn-tint",
    "dup-warn-bg": "warn-tint",
    "dup-warn-border": "warn-ink",
    "success-bg": "success-fill",
    "success-fg": "on-record",
}

# Every Python Pango / GSK role mapped to its target --cx-* token.
PYTHON_ROLE_TOKENS: dict[str, str] = {
    "played-word-bg": "highlight",
    "played-word-fg": "highlight-ink",
    "search-hit-bg": "highlight",
    "search-hit-selected-bg": "highlight",
    "search-hit-fg": "highlight-ink",
    "aside-chip-bg": "ink",
    "spectro-pill-ink": "record-ink",
    "spectro-row-ink": "ink",
    "spectro-pill-silent": "warn-ink",
    "spectro-row-silent": "warn-ink",
    "timeline-tick": "accent",
}

# Six speaker slots plus stone (0 = unnamed, 1..6 = order of first speaking).
SPEAKER_ROLE_TOKENS: dict[str, str] = {
    **{f"speaker-{slot}": f"speaker-{slot}" for slot in range(7)},
    **{f"avatar-{slot}": f"avatar-{slot}" for slot in range(7)},
}

ROLE_TOKENS: dict[str, str] = (
    {f"adw:{name}": target for name, target in ADW_ROLE_TOKENS.items()}
    | CSS_ROLE_TOKENS
    | PYTHON_ROLE_TOKENS
    | SPEAKER_ROLE_TOKENS
)

# All Wave 2 card selection & neutral surface wash roles are resolved onto --cx-* tokens.
DEFERRED_W2_ROLES: frozenset[str] = frozenset()

# All WP1 foundation roles are active.
UNAPPLIED_W1_ROLES: frozenset[str] = frozenset()


def token(name: str, *, dark: bool = False) -> str:
    light_hex, dark_hex = (TOKENS | DERIVED_TOKENS)[name]
    return dark_hex if dark else light_hex


def speaker_slot(speaker_or_index: object, *, provenance: str | None = None) -> int:
    """Return 0 for unnamed speakers, or 1..6 from first-speech colourIndex (HARMONY 10.11)."""
    if isinstance(speaker_or_index, int):
        colour_index = speaker_or_index
    else:
        stored = getattr(speaker_or_index, "stored_colour_index", None)
        colour_index = int(stored if stored is not None else getattr(speaker_or_index, "colour_index", 0))
        if provenance is None:
            provenance = getattr(speaker_or_index, "provenance", None)
            if provenance is None:
                name = getattr(speaker_or_index, "name", None)
                rank = int(getattr(speaker_or_index, "colour_index", 0))
                if isinstance(name, str) and (
                    re.fullmatch(r"Speaker \d+", name) or name == text("common.speaker.unnamed", n=rank + 1)
                ):
                    provenance = "unnamed"
    if provenance == "unnamed":
        return 0
    return (colour_index % 6) + 1


def saved_voice_slot(voice_or_order: object) -> int:
    """Return 1..6 from a saved voice's 0-based saveOrder (HARMONY 10.12)."""
    if isinstance(voice_or_order, int):
        order = voice_or_order
    elif isinstance(voice_or_order, dict):
        order = int(voice_or_order.get("saveOrder", 0))
    else:
        order = int(getattr(voice_or_order, "save_order", 0))
    return (order % 6) + 1


def speaker_colour(speaker_or_index: object, *, dark: bool = False, provenance: str | None = None) -> str:
    return token(f"speaker-{speaker_slot(speaker_or_index, provenance=provenance)}", dark=dark)


def speaker_avatar_wash(speaker_or_index: object, *, dark: bool = False, provenance: str | None = None) -> str:
    return token(f"avatar-{speaker_slot(speaker_or_index, provenance=provenance)}", dark=dark)


def played_word_colours(*, dark: bool = False) -> tuple[str, str]:
    return (token("highlight", dark=dark), token("highlight-ink", dark=dark))


def aside_chip_wash(*, dark: bool = False) -> tuple[str, str]:
    return (token("ink", dark=dark), "10%")


def search_hit_colours(*, dark: bool = False, selected: bool = False) -> tuple[str, str]:
    _ = selected
    return (token("highlight", dark=dark), token("highlight-ink", dark=dark))


def spectrogram_colours(kind: str, *, dark: bool = False, silent: bool = False) -> tuple[str, str]:
    ink = token("record-ink" if kind == "pill" else "ink", dark=dark)
    if not silent:
        return (ink, ink)
    return (ink, token("warn-ink", dark=dark))


def timeline_tick_colour(*, dark: bool = False) -> str | None:
    return token("accent", dark=dark)


__all__ = [
    "ADW_ROLE_TOKENS",
    "CSS_ROLE_TOKENS",
    "DEFERRED_W2_ROLES",
    "DERIVED_TOKENS",
    "PYTHON_ROLE_TOKENS",
    "ROLE_TOKENS",
    "SPEAKER_ROLE_TOKENS",
    "TOKENS",
    "UNAPPLIED_W1_ROLES",
    "UPSTREAM_COMMIT",
    "aside_chip_wash",
    "played_word_colours",
    "saved_voice_slot",
    "search_hit_colours",
    "speaker_avatar_wash",
    "speaker_colour",
    "speaker_slot",
    "spectrogram_colours",
    "timeline_tick_colour",
    "token",
]
