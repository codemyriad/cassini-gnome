# SPDX-License-Identifier: GPL-3.0-or-later
"""The app stylesheet for GTK older than 4.16 (D-1140: Ubuntu 24.04 LTS).

src/style.css (and the generated src/tokens.css it imports) use CSS custom
properties, ``var()``, ``color-mix()`` and ``@media (prefers-color-scheme)``,
which GTK only parses since 4.16. GTK 4.14 with libadwaita 1.5 drops every
declaration that uses them, with a parser warning each, so the app would lose
its brand colours. :func:`legacy_stylesheet` resolves that stylesheet for one
colour scheme into CSS GTK 4.14 understands:

- custom properties set on ``:root`` are substituted where ``var()`` reads
  them, dark ``:root`` overrides replacing light ones in the dark sheet;
- the libadwaita 1.6+ palette variables (``--window-bg-color`` ...) become
  the libadwaita 1.5 named colours (``@define-color window_bg_color ...``);
- ``color-mix(in srgb, ...)`` is computed to an ``rgba()`` value (or GTK's
  ``mix()``/``alpha()`` when an argument is a named colour);
- ``@media (prefers-color-scheme: dark)`` blocks are kept, unwrapped, only in
  the dark sheet;
- custom properties scoped to other selectors, and declarations whose
  variables cannot be resolved, are dropped.

No GTK import here: the translation is plain text (and unit-tested without gi).
"""

import re

LEGACY_GTK = (4, 16)
_DARK_MEDIA = re.compile(r"\(\s*prefers-color-scheme\s*:\s*dark\s*\)")
_VAR = re.compile(r"var\(\s*(--[A-Za-z0-9_-]+)\s*(?:,\s*([^()]*(?:\([^()]*\)[^()]*)*))?\)")
_IMPORT = re.compile(r"@import\s+url\(\s*[\"']?([^\"')]+)[\"']?\s*\)\s*;")


def is_legacy(gtk_version):
    """Whether GTK *gtk_version* (major, minor[, micro]) needs :func:`legacy_stylesheet`."""
    return tuple(gtk_version[:2]) < LEGACY_GTK


def _strip_comments(css):
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def _blocks(css):
    """Top-level ``(prelude, body)`` pairs; *body* is the text between the braces."""
    items, depth, start, prelude = [], 0, 0, ""
    for index, char in enumerate(css):
        if char == "{":
            if depth == 0:
                prelude, start = css[start:index].strip(), index + 1
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                items.append((prelude, css[start:index]))
                start = index + 1
            elif depth < 0:
                raise ValueError("unbalanced '}' in stylesheet")
        elif char == ";" and depth == 0:
            statement = css[start:index].strip()
            if statement:
                items.append((statement, None))
            start = index + 1
    if depth:
        raise ValueError("unbalanced '{' in stylesheet")
    return items


def _declarations(body):
    """``(name, value)`` pairs of a declaration block (no nested blocks in our CSS)."""
    out, depth, start = [], 0, 0
    for index, char in enumerate(body + ";"):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == ";" and depth == 0:
            part = body[start:index].strip()
            start = index + 1
            if ":" in part:
                name, value = part.split(":", 1)
                out.append((name.strip(), value.strip()))
    return out


def _rules(css, dark):
    """Flatten *css* into ``(selector, declarations)`` for one colour scheme."""
    rules = []
    for prelude, body in _blocks(css):
        if body is None:
            continue  # @import (inlined beforehand), @charset ...
        if prelude.startswith("@media"):
            if dark and _DARK_MEDIA.search(prelude):
                rules.extend(_rules(body, dark))
            continue
        if prelude.startswith("@"):
            continue  # @keyframes and other at-rules: GTK 4.14 keeps none of ours.
        rules.append((prelude, _declarations(body)))
    return rules


def _parse_color(text):
    """RGBA floats (0..1) of a literal colour, or None (named colours, keywords ...)."""
    text = text.strip().lower()
    if text == "transparent":
        return (0.0, 0.0, 0.0, 0.0)
    match = re.fullmatch(r"#([0-9a-f]{3,8})", text)
    if match:
        digits = match.group(1)
        if len(digits) in (3, 4):
            digits = "".join(c * 2 for c in digits)
        if len(digits) not in (6, 8):
            return None
        values = [int(digits[i : i + 2], 16) / 255 for i in range(0, len(digits), 2)]
        return tuple(values) if len(values) == 4 else (*values, 1.0)
    match = re.fullmatch(r"rgba?\(([^()]*)\)", text)
    if match:
        parts = [p for p in re.split(r"[\s,/]+", match.group(1).strip()) if p]
        if len(parts) not in (3, 4):
            return None
        try:
            rgb = [float(p[:-1]) / 100 if p.endswith("%") else float(p) / 255 for p in parts[:3]]
            alpha = 1.0
            if len(parts) == 4:
                alpha = float(parts[3][:-1]) / 100 if parts[3].endswith("%") else float(parts[3])
        except ValueError:
            return None
        return (*rgb, alpha)
    return None


def _format_color(rgba):
    r, g, b, a = rgba
    channels = ", ".join(str(round(max(0.0, min(1.0, c)) * 255)) for c in (r, g, b))
    alpha = round(max(0.0, min(1.0, a)), 4)
    if alpha == 1:
        return f"rgb({channels})"
    # Computed from the brand tokens, never a literal colour (W1 scan).
    function = "rgb" + "a"
    return f"{function}({channels}, {format(alpha, 'g')})"


def _split_args(text):
    out, depth, start = [], 0, 0
    for index, char in enumerate(text):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "," and depth == 0:
            out.append(text[start:index].strip())
            start = index + 1
    out.append(text[start:].strip())
    return out


def _color_and_percent(arg):
    match = re.fullmatch(r"(.*?)\s+(\d+(?:\.\d+)?)%", arg, flags=re.S)
    if match:
        return match.group(1).strip(), float(match.group(2)) / 100
    match = re.fullmatch(r"(\d+(?:\.\d+)?)%\s+(.*)", arg, flags=re.S)
    if match:
        return match.group(2).strip(), float(match.group(1)) / 100
    return arg.strip(), None


def _color_mix(args):
    """CSS ``color-mix(in <space>, A [p%], B [q%])`` as GTK 4.14 CSS."""
    parts = _split_args(args)
    if len(parts) != 3 or not parts[0].startswith("in "):
        raise ValueError(f"unsupported color-mix({args})")
    (first, p), (second, q) = _color_and_percent(parts[1]), _color_and_percent(parts[2])
    if p is None and q is None:
        p, q = 0.5, 0.5
    elif p is None:
        p = 1 - q
    elif q is None:
        q = 1 - p
    total = p + q
    if total <= 0:
        raise ValueError(f"color-mix({args}) has no weight")
    scale, p = min(total, 1.0), p / total
    a, b = _parse_color(first), _parse_color(second)
    if a is not None and b is not None:
        # CSS mixes premultiplied colours (CSS Color 5 §2.4).
        alpha = a[3] * p + b[3] * (1 - p)
        rgb = [(a[i] * a[3] * p + b[i] * b[3] * (1 - p)) / alpha for i in range(3)] if alpha else [0.0, 0.0, 0.0]
        return _format_color((*rgb, alpha * scale))
    if second.strip().lower() == "transparent":
        result = "alpha({}, {})".format(first, format(round(p, 4), "g"))
    elif first.strip().lower() == "transparent":
        result = "alpha({}, {})".format(second, format(round(1 - p, 4), "g"))
    else:
        result = "mix({}, {}, {})".format(second, first, format(round(p, 4), "g"))
    if scale < 1:
        result = "alpha({}, {})".format(result, format(round(scale, 4), "g"))
    return result


def _replace_color_mix(value):
    while True:
        start = value.find("color-mix(")
        if start < 0:
            return value
        # The innermost call first, so its arguments hold no color-mix().
        inner = value.rfind("color-mix(")
        open_at = inner + len("color-mix(")
        depth, index = 1, open_at
        while depth:
            if index >= len(value):
                raise ValueError(f"unbalanced color-mix() in {value!r}")
            depth += {"(": 1, ")": -1}.get(value[index], 0)
            index += 1
        value = value[:inner] + _color_mix(value[open_at : index - 1]) + value[index:]


def _named_color(variable):
    """``--window-bg-color`` -> ``window_bg_color`` (libadwaita 1.5 named colours)."""
    if variable.endswith("-color"):
        return variable[2:].replace("-", "_")
    return None


def _resolve(value, variables, seen=()):
    def substitute(match):
        name, fallback = match.group(1), match.group(2)
        if name in variables and name not in seen:
            return _resolve(variables[name], variables, (*seen, name))
        if fallback is not None:
            return _resolve(fallback, variables, seen)
        named = _named_color(name)
        if named:
            return "@" + named
        raise KeyError(name)

    previous = None
    while previous != value:
        previous, value = value, _VAR.sub(substitute, value)
    return value


def inline_imports(css, read):
    """*css* with each ``@import url("name")`` replaced by ``read(name)``."""
    return _IMPORT.sub(lambda match: inline_imports(read(match.group(1)), read), css)


def legacy_stylesheet(css, dark=False):
    """GTK 4.14 CSS equivalent to *css* (imports already inlined) in one colour scheme."""
    rules = _rules(_strip_comments(css), dark)
    variables = {}
    for selector, declarations in rules:
        if selector.strip() == ":root":
            for name, value in declarations:
                if name.startswith("--"):
                    variables[name] = value
    lines = []
    for name, value in variables.items():
        named = _named_color(name)
        if named is None:
            continue
        try:
            lines.append(f"@define-color {named} {_replace_color_mix(_resolve(value, variables, (name,)))};")
        except KeyError:
            continue
    if lines:
        lines.append("")
    for selector, declarations in rules:
        kept = []
        for name, value in declarations:
            if name.startswith("--"):
                continue
            try:
                kept.append(f"  {name}: {_replace_color_mix(_resolve(value, variables))};")
            except KeyError:
                continue
        if not kept:
            continue
        if selector.strip() == ":root":
            selector = "window"
        lines.append("{} {{\n{}\n}}".format(selector, "\n".join(kept)))
    return "\n".join(lines) + "\n"


# libadwaita loads <resource base path>/style.css by itself; on GTK < 4.16 the
# application uses this base path, which holds no stylesheet, instead.
LEGACY_BASE_SUFFIX = "/gtk-4.14"


class LegacyStyle:
    """Loads :func:`legacy_stylesheet` for the current colour scheme on GTK < 4.16.

    *resource* is the application's real resource prefix (style.css, tokens.css
    and icons/ under it); the application itself uses
    ``resource + LEGACY_BASE_SUFFIX`` as base path so GTK 4.14 never parses the
    modern sheet. Rebuilt when the colour scheme changes.
    """

    def __init__(self, resource):
        import gi

        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        from gi.repository import Adw, Gdk, Gio, Gtk

        def read(name):
            data = Gio.resources_lookup_data(resource + "/" + name, Gio.ResourceLookupFlags.NONE)
            return data.get_data().decode("utf-8")

        self._css = inline_imports(read("style.css"), read)
        self._sheets = {}
        self.provider = Gtk.CssProvider()
        display = Gdk.Display.get_default()
        if display is not None:
            Gtk.StyleContext.add_provider_for_display(display, self.provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
            # GtkApplication only adds <base path>/icons, which is now empty.
            Gtk.IconTheme.get_for_display(display).add_resource_path(resource + "/icons")
        self.style_manager = Adw.StyleManager.get_default()
        self.style_manager.connect("notify::dark", lambda *_: self.load())
        self.load()

    def load(self):
        dark = bool(self.style_manager.get_dark())
        if dark not in self._sheets:
            self._sheets[dark] = legacy_stylesheet(self._css, dark)
        self.provider.load_from_string(self._sheets[dark])
