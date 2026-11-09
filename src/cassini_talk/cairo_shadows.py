# SPDX-License-Identifier: GPL-3.0-or-later
"""Reuse static shadow masks while Cairo paints a moving or fading sheet."""

from collections import OrderedDict

from gi.repository import Graphene, Gsk, Gtk


class CairoShadows:
    """Cache immutable shadows; keep content, clips and animation nodes live."""

    def __init__(self):
        self.textures = OrderedDict()
        self.bytes = 0

    def clear(self):
        self.textures.clear()
        self.bytes = 0
        return False

    def snapshot(self, widget, snapshot):
        renderer = widget.get_native().get_renderer()
        if not isinstance(renderer, Gsk.CairoRenderer):
            Gtk.Widget.do_snapshot(widget, snapshot)
            return
        source = Gtk.Snapshot.new()
        Gtk.Widget.do_snapshot(widget, source)
        node = source.to_node()
        if node is None:
            return
        node = self._node(node, renderer, widget.get_scale_factor())
        snapshot.append_node(node)

    def _node(self, node, renderer, scale):
        if isinstance(node, Gsk.OutsetShadowNode):
            key = hash(renderer), scale, node.serialize().get_data()
            if key in self.textures:
                self.textures.move_to_end(key)
                return self.textures[key][0]
            bounds = node.get_bounds()
            pixels = (bounds.size.width * scale + 1) * (bounds.size.height * scale + 1)
            if pixels * 4 > 16 * 1024 * 1024:
                return node
            scaled = Gsk.TransformNode.new(node, Gsk.Transform.new().scale(scale, scale))
            viewport = Graphene.Rect().init(
                bounds.origin.x * scale, bounds.origin.y * scale, bounds.size.width * scale, bounds.size.height * scale
            )
            texture = renderer.render_texture(scaled, viewport)
            size = texture.get_width() * texture.get_height() * 4
            while self.textures and (len(self.textures) >= 4 or self.bytes + size > 32 * 1024 * 1024):
                _key, (_node, old_size) = self.textures.popitem(last=False)
                self.bytes -= old_size
            cached = Gsk.TextureNode.new(texture, bounds)
            self.textures[key] = cached, size
            self.bytes += size
            return cached
        if isinstance(node, Gsk.ContainerNode):
            children = [node.get_child(i) for i in range(node.get_n_children())]
            updated = [self._node(child, renderer, scale) for child in children]
            return (
                Gsk.ContainerNode.new(updated)
                if any(a is not b for a, b in zip(children, updated, strict=True))
                else node
            )
        for kind, parameter in (
            (Gsk.TransformNode, "get_transform"),
            (Gsk.OpacityNode, "get_opacity"),
            (Gsk.ClipNode, "get_clip"),
            (Gsk.RoundedClipNode, "get_clip"),
        ):
            if isinstance(node, kind):
                child = node.get_child()
                updated = self._node(child, renderer, scale)
                return kind.new(updated, getattr(node, parameter)()) if updated is not child else node
        return node
