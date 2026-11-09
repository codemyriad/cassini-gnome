# SPDX-License-Identifier: GPL-3.0-or-later
"""Compare cached shadows against Cairo's original nodes, without a display."""

import pytest

gi = pytest.importorskip("gi")
try:
    for namespace in ("Gtk", "Gdk", "Gsk"):
        gi.require_version(namespace, "4.0")
    gi.require_version("Graphene", "1.0")
except ValueError:
    pytest.skip("GTK introspection is unavailable", allow_module_level=True)
from gi.repository import Gdk, Graphene, Gsk  # noqa: E402

from cassini_talk.cairo_shadows import CairoShadows  # noqa: E402


@pytest.fixture
def renderer():
    renderer = Gsk.CairoRenderer.new()
    renderer.realize(None)
    yield renderer
    renderer.unrealize()


def shadow(width=130, color="rgba(0,0,0,0.2)"):
    outline = Gsk.RoundedRect()
    outline.init_from_rect(Graphene.Rect().init(0, 0, width, 90), 12)
    rgba = Gdk.RGBA()
    rgba.parse(color)
    return Gsk.OutsetShadowNode.new(outline, rgba, 0, 8, 2, 20)


def pixels(renderer, node, viewport):
    texture = renderer.render_texture(node, viewport)
    downloader = Gdk.TextureDownloader.new(texture)
    downloader.set_format(Gdk.MemoryFormat.R8G8B8A8_PREMULTIPLIED)
    data, stride = downloader.download_bytes()
    return (texture.get_width(), texture.get_height(), stride), data.get_data()


def scaled(node, factor):
    return Gsk.TransformNode.new(node, Gsk.Transform.new().scale(factor, factor))


def test_static_shadows_are_pixel_exact_across_theme_geometry_and_dpi(renderer):
    cache = CairoShadows()
    for color in ("rgba(0,0,0,0.2)", "rgba(0.3,0.1,0.2,0.4)"):
        for width in (130, 171):
            for dpi in (1, 2):
                original = shadow(width, color)
                cached = cache._node(original, renderer, dpi)
                reference, actual = scaled(original, dpi), scaled(cached, dpi)
                assert pixels(renderer, reference, reference.get_bounds()) == pixels(
                    renderer, actual, reference.get_bounds()
                )


@pytest.mark.parametrize("factor", [0.8, 0.95, 1.03])
@pytest.mark.parametrize("dpi", [1, 2])
def test_animated_shadow_keeps_geometry_and_nearly_identical_pixels(renderer, factor, dpi):
    cache = CairoShadows()
    original = Gsk.OpacityNode.new(scaled(shadow(color="rgba(0,0,0,0.4)"), factor), 0.6)
    bounds = original.get_bounds()
    clip = Graphene.Rect().init(bounds.origin.x, bounds.origin.y + 4, bounds.size.width - 4, bounds.size.height - 4)
    original = Gsk.ClipNode.new(original, clip)
    cached = cache._node(original, renderer, dpi)
    reference, actual = scaled(original, dpi), scaled(cached, dpi)
    shape_a, bytes_a = pixels(renderer, reference, reference.get_bounds())
    shape_b, bytes_b = pixels(renderer, actual, reference.get_bounds())
    assert shape_a == shape_b
    # Fractional interpolation of a pre-rasterized soft shadow differs slightly
    # from Cairo's repeated one-pixel masks. Static frames above remain exact.
    error = sum(abs(a - b) for a, b in zip(bytes_a, bytes_b, strict=True)) / len(bytes_a)
    assert error < 0.2  # Mean channel error on a 0..255 scale.


def test_repeated_resizing_has_bounded_cache_and_clear_releases_it(renderer):
    cache = CairoShadows()
    for width in range(130, 150):
        node = shadow(width)
        cache._node(node, renderer, 1)
        assert len(cache.textures) <= 4
        assert cache.bytes <= 32 * 1024 * 1024
    assert cache.textures and cache.bytes
    assert cache.clear() is False
    assert not cache.textures and cache.bytes == 0
