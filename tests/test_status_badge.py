# SPDX-License-Identifier: GPL-3.0-or-later
"""W2.1 status badge unit and headless GSK glyph tests (GNOME-COM-02, GNOME-COM-03)."""

import pytest

from cassini_talk import tokens

gi = pytest.importorskip("gi")
try:
    for namespace in ("Gtk", "Gdk", "Gsk"):
        gi.require_version(namespace, "4.0")
    gi.require_version("Graphene", "1.0")
except ValueError:
    pytest.skip("GTK introspection is unavailable", allow_module_level=True)
from gi.repository import Gdk, Graphene, Gsk, Gtk  # noqa: E402

from cassini_talk import status_badge  # noqa: E402


@pytest.fixture
def cairo_renderer():
    renderer = Gsk.CairoRenderer.new()
    renderer.realize(None)
    yield renderer
    renderer.unrealize()


def test_badge_constants_kind_mapping_and_applied_token_roles():
    assert status_badge.BADGE_HEIGHT_PX == 22
    assert status_badge.BADGE_PAD_X_PX == 8
    assert status_badge.BADGE_GAP_PX == 4
    assert status_badge.BADGE_RADIUS_PX == 999
    assert status_badge.BADGE_FONT_PX == 12
    assert status_badge.BADGE_FONT_WEIGHT == 600
    assert status_badge.GLYPH_SIZE_PX == {
        "dot": 6,
        "spinner": 10,
        "alert": 11,
        "incoming": 11,
    }
    assert status_badge.KIND_GLYPH == {
        "recording": "dot",
        "importing": "spinner",
        "saving": "spinner",
        "transcribing": "spinner",
        "needsAttention": "alert",
        "interrupted": "alert",
        "received": "incoming",
    }
    assert status_badge.normalize_badge_kind("attention") == "needsAttention"
    assert status_badge.normalize_glyph_kind("arrow") == "incoming"
    assert status_badge.normalize_glyph_kind("incoming") == "incoming"
    for canonical in status_badge.KIND_GLYPH:
        assert status_badge.normalize_badge_kind(canonical) == canonical
    with pytest.raises(ValueError, match="Unknown status badge kind"):
        status_badge.normalize_badge_kind("unknown")
    with pytest.raises(ValueError, match="Unknown badge glyph kind"):
        status_badge.normalize_glyph_kind("unknown")

    badge_roles = {
        "badge-red-bg",
        "badge-red-fg",
        "badge-transcribing-bg",
        "badge-transcribing-fg",
        "badge-transcribing-spinner",
        "badge-amber-bg",
        "badge-amber-fg",
        "badge-grey-bg",
        "badge-grey-fg",
        "badge-received-fg",
        "badge-received-ring",
    }
    assert badge_roles.issubset(tokens.CSS_ROLE_TOKENS)
    assert tokens.CSS_ROLE_TOKENS["badge-transcribing-spinner"] == "accent"
    assert not tokens.UNAPPLIED_W1_ROLES


@pytest.mark.parametrize("glyph_kind", ("dot", "spinner", "alert", "incoming", "arrow"))
def test_headless_gsk_glyph_primitives_render_visible_currentcolor_ink(cairo_renderer, glyph_kind):
    canonical = status_badge.normalize_glyph_kind(glyph_kind)
    size = status_badge.GLYPH_SIZE_PX[canonical]
    color = Gdk.RGBA()
    color.parse("rgb(192,28,40)")

    snap = Gtk.Snapshot.new()
    status_badge.snapshot_badge_glyph(snap, glyph_kind, size, color)
    node = snap.to_node()
    assert node is not None
    viewport = Graphene.Rect().init(0, 0, size, size)
    texture = cairo_renderer.render_texture(node, viewport)

    downloader = Gdk.TextureDownloader.new(texture)
    downloader.set_format(Gdk.MemoryFormat.R8G8B8A8_PREMULTIPLIED)
    gbytes, stride = downloader.download_bytes()
    buf = gbytes.get_data()
    alphas = [buf[y * stride + x * 4 + 3] for y in range(size) for x in range(size)]
    assert max(alphas) >= 200
    if glyph_kind == "spinner":
        # Track ring renders semi-transparent (~0.3 * 255 = 76) alongside full-alpha arc (~255).
        assert any(40 <= a <= 130 for a in alphas)


def test_snapshot_badge_glyph_zero_box_and_centering(cairo_renderer):
    color = Gdk.RGBA()
    color.parse("rgb(20,20,19)")

    empty = Gtk.Snapshot.new()
    status_badge.snapshot_badge_glyph(empty, "dot", 6, color, width=0, height=6)
    assert empty.to_node() is None

    centered = Gtk.Snapshot.new()
    status_badge.snapshot_badge_glyph(centered, "dot", 6, color, width=10, height=10)
    node = centered.to_node()
    assert node is not None
    texture = cairo_renderer.render_texture(node, Graphene.Rect().init(0, 0, 10, 10))
    downloader = Gdk.TextureDownloader.new(texture)
    downloader.set_format(Gdk.MemoryFormat.R8G8B8A8_PREMULTIPLIED)
    gbytes, stride = downloader.download_bytes()
    buf = gbytes.get_data()
    # Outer corner stays transparent; center pixel carries opaque ink.
    assert buf[0 * stride + 0 * 4 + 3] == 0
    assert buf[5 * stride + 5 * 4 + 3] == 255
