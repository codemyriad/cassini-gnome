# SPDX-License-Identifier: GPL-3.0-or-later
"""App-wide semantic colour role mapping backed by generated _brand_tokens.py (W1.1)."""

from ._brand_tokens import DERIVED_TOKENS, TOKENS, UPSTREAM_COMMIT

# Libadwaita 1.8 :root palette variables mapped to their target --cx-* token.
ADW_ROLE_TOKENS: dict[str, str] = {
    "window-bg-color": "ground",
    "window-fg-color": "ink",
    "view-bg-color": "surface",
    "view-fg-color": "ink",
    "headerbar-bg-color": "ground",
    "headerbar-fg-color": "ink",
    "headerbar-backdrop-color": "ground",
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
    "sidebar-bg": "sunken",
    "sidebar-fg": "ink",
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
    "badge-amber-bg": "warn-tint",
    "badge-amber-fg": "warn-ink",
    "badge-grey-bg": "ink",
    "badge-grey-fg": "ink-2",
    "badge-received-fg": "ink-2",
    "badge-received-ring": "line",
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
    "aside-chip-bg": "neutral-tint",
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

# Roles intentionally deferred beyond Wave 1 (C16 sidebar choice, W2.3 row lift, GNOME-TOK-02 grey washes).
DEFERRED_W2_ROLES: frozenset[str] = frozenset(
    {
        "sidebar-bg",
        "sidebar-fg",
        "wash-subtle",
        "wash-chip",
        "wash-trough",
        "row-selected-bg",
        "search-selected-bg",
        "aside-chip-bg",
    }
)

# Badge CSS roles applied in W2.1 (GNOME-COM-02, GNOME-COM-03).
APPLIED_W2_BADGE_ROLES: frozenset[str] = frozenset(
    {
        "badge-red-bg",
        "badge-red-fg",
        "badge-transcribing-bg",
        "badge-transcribing-fg",
        "badge-amber-bg",
        "badge-amber-fg",
        "badge-grey-bg",
        "badge-grey-fg",
        "badge-received-fg",
        "badge-received-ring",
    }
)

# Decided Wave 1 roles not yet switched in W1.1 plumbing (left empty in W1 look preview).
UNAPPLIED_W1_ROLES: frozenset[str] = frozenset(ROLE_TOKENS) - DEFERRED_W2_ROLES - APPLIED_W2_BADGE_ROLES


def token(name: str, *, dark: bool = False) -> str:
    light_hex, dark_hex = (TOKENS | DERIVED_TOKENS)[name]
    return dark_hex if dark else light_hex


def speaker_slot(speaker_or_index: object, *, provenance: str | None = None) -> int:
    """Return 0 for unnamed speakers, or 1..6 from colour_index."""
    if isinstance(speaker_or_index, int):
        colour_index = speaker_or_index
    else:
        colour_index = int(getattr(speaker_or_index, "colour_index", 0))
        if provenance is None:
            provenance = getattr(speaker_or_index, "provenance", None)
    if provenance == "unnamed":
        return 0
    return (colour_index % 6) + 1


def speaker_avatar_wash(speaker_or_index: object, *, dark: bool = False, provenance: str | None = None) -> str:
    return token(f"avatar-{speaker_slot(speaker_or_index, provenance=provenance)}", dark=dark)


# BEGIN PRE-W1 LEGACY FALLBACKS
SPEAKER_COLOURS = ("#1c71d8", "#b34d00", "#813d9c", "#21804b", "#bd2028", "#167c8b")
SPEAKER_DARK_COLOURS = ("#78aeed", "#ffa348", "#ce91df", "#77d39a", "#ff9298", "#79c7d1")


def speaker_colour(speaker_or_index: object, *, dark: bool = False, provenance: str | None = None) -> str:
    _ = provenance
    colour_index = (
        speaker_or_index if isinstance(speaker_or_index, int) else int(getattr(speaker_or_index, "colour_index", 0))
    )
    palette = SPEAKER_DARK_COLOURS if dark else SPEAKER_COLOURS
    return palette[colour_index % len(palette)]


def played_word_colours(*, dark: bool = False) -> tuple[str, str]:
    _ = dark
    return ("#fbe7a1", "#202024")


def aside_chip_wash(*, dark: bool = False) -> tuple[str, str]:
    return ("#ffffff", "10%") if dark else ("#000006", "7%")


def search_hit_colours(*, dark: bool = False, selected: bool = False) -> tuple[str, str]:
    _ = dark
    return ("#ffffff" if selected else "#fbe7a1", "#202024")


def spectrogram_colours(kind: str, *, dark: bool = False, silent: bool = False) -> tuple[str, str]:
    ink = ("#ff8a8e" if dark else "#c01c28") if kind == "pill" else ("#ffffff" if dark else "#2e2e32")
    if not silent:
        return (ink, ink)
    bg = "#f8e45c" if dark else ("#e5a50a" if kind == "pill" else "#c58a00")
    return (ink, bg)


# END PRE-W1 LEGACY FALLBACKS


def timeline_tick_colour(*, dark: bool = False) -> str | None:
    _ = dark
    return None


__all__ = [
    "ADW_ROLE_TOKENS",
    "APPLIED_W2_BADGE_ROLES",
    "CSS_ROLE_TOKENS",
    "DEFERRED_W2_ROLES",
    "DERIVED_TOKENS",
    "PYTHON_ROLE_TOKENS",
    "ROLE_TOKENS",
    "SPEAKER_COLOURS",
    "SPEAKER_DARK_COLOURS",
    "SPEAKER_ROLE_TOKENS",
    "TOKENS",
    "UNAPPLIED_W1_ROLES",
    "UPSTREAM_COMMIT",
    "aside_chip_wash",
    "played_word_colours",
    "search_hit_colours",
    "speaker_avatar_wash",
    "speaker_colour",
    "speaker_slot",
    "spectrogram_colours",
    "timeline_tick_colour",
    "token",
]
