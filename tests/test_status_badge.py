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
    assert status_badge.BADGE_FONT_PX == 13
    assert status_badge.BADGE_FONT_WEIGHT == 600
    assert status_badge.GLYPH_SIZE_PX == {
        "dot": 6,
        "spinner": 10,
        "alert": 11,
        "arrow": 11,
    }
    assert status_badge.KIND_GLYPH == {
        "recording": "dot",
        "importing": "spinner",
        "saving": "spinner",
        "transcribing": "spinner",
        "needsAttention": "alert",
        "interrupted": "alert",
        "received": "arrow",
    }
    assert status_badge.normalize_badge_kind("attention") == "needsAttention"
    with pytest.raises(ValueError, match="Unknown status badge kind"):
        status_badge.normalize_badge_kind("unknown")

    assert tokens.APPLIED_W2_BADGE_ROLES.issubset(tokens.CSS_ROLE_TOKENS)
    assert tokens.UNAPPLIED_W1_ROLES.isdisjoint(tokens.APPLIED_W2_BADGE_ROLES)


@pytest.mark.parametrize("glyph_kind", ("dot", "spinner", "alert", "arrow"))
def test_headless_gsk_glyph_primitives_render_visible_currentcolor_ink(cairo_renderer, glyph_kind):
    size = status_badge.GLYPH_SIZE_PX[glyph_kind]
    prims = status_badge._glyph_primitives()[glyph_kind]
    color = Gdk.RGBA()
    color.parse("rgb(192,28,40)")

    snap = Gtk.Snapshot.new()
    if glyph_kind == "dot":
        (circle,) = prims
        snap.append_fill(circle, Gsk.FillRule.WINDING, color)
    elif glyph_kind == "spinner":
        ring, arc, ring_stroke, arc_stroke = prims
        track = Gdk.RGBA()
        track.red, track.green, track.blue, track.alpha = (
            color.red,
            color.green,
            color.blue,
            status_badge.BADGE_SPINNER_TRACK_ALPHA,
        )
        scale = size / 16.0
        snap.scale(scale, scale)
        snap.append_stroke(ring, ring_stroke, track)
        snap.append_stroke(arc, arc_stroke, color)
    elif glyph_kind == "alert":
        ring, stem, dot, ring_stroke, stem_stroke = prims
        scale = size / 16.0
        snap.scale(scale, scale)
        snap.append_stroke(ring, ring_stroke, color)
        snap.append_stroke(stem, stem_stroke, color)
        snap.append_fill(dot, Gsk.FillRule.WINDING, color)
    else:
        arrow, arrow_stroke = prims
        scale = size / 24.0
        snap.scale(scale, scale)
        snap.append_stroke(arrow, arrow_stroke, color)

    node = snap.to_node()
    assert node is not None
    viewport = Graphene.Rect().init(0, 0, size, size)
    texture = cairo_renderer.render_texture(node, viewport)
    raw = texture.save_to_tiff_bytes().get_data()
    assert len(raw) > 64

    # Verify non-transparent pixels exist inside the glyph bounds.
    downloader = Gdk.TextureDownloader.new(texture)
    downloader.set_format(Gdk.MemoryFormat.R8G8B8A8)
    gbytes, stride = downloader.download_bytes()
    buf = gbytes.get_data()
    alphas = [buf[y * stride + x * 4 + 3] for y in range(size) for x in range(size)]
    assert max(alphas) >= 200
    if glyph_kind == "spinner":
        # Track ring renders semi-transparent (~0.3 * 255 = 76) alongside full-alpha arc (~255).
        assert any(40 <= a <= 130 for a in alphas)


def test_status_badge_widget_tree_and_live_label_mutation():
    if not Gtk.init_check() or Gdk.Display.get_default() is None:
        pytest.skip("No GTK display server available")
    for kind, expected_glyph, expected_size in (
        ("recording", "dot", 6),
        ("importing", "spinner", 10),
        ("saving", "spinner", 10),
        ("transcribing", "spinner", 10),
        ("needsAttention", "alert", 11),
        ("attention", "alert", 11),
        ("interrupted", "alert", 11),
        ("received", "arrow", 11),
    ):
        badge = status_badge.StatusBadge(kind, "Initial")
        canonical = "needsAttention" if kind == "attention" else kind
        assert badge.kind == canonical
        assert badge.glyph_kind == expected_glyph
        assert "badge" in badge.get_css_classes()
        assert f"badge-kind-{canonical}" in badge.get_css_classes()
        assert badge.glyph_widget.get_accessible_role() == Gtk.AccessibleRole.PRESENTATION
        assert not badge.glyph_widget.get_can_target()
        min_w, nat_w, _, _ = badge.glyph_widget.measure(Gtk.Orientation.HORIZONTAL, -1)
        min_h, nat_h, _, _ = badge.glyph_widget.measure(Gtk.Orientation.VERTICAL, -1)
        assert (min_w, nat_w, min_h, nat_h) == (expected_size, expected_size, expected_size, expected_size)
        assert badge.get_label() == "Initial"
        badge.set_label("Recording 1:05")
        assert badge.get_label() == "Recording 1:05"
        assert badge.get_text() == "Recording 1:05"
