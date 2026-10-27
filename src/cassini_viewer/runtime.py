# runtime.py
#
# Copyright 2026 Silvio Tomatis
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Startup check for the libraries Cassini needs.

The launcher calls check() before importing any window code, so a missing or
too-old library ends in one readable message instead of a traceback. Keep this
module importable on any Python 3 without PyGObject: no gi import at the top
level and no syntax newer than the oldest Python we want to report on.
"""

import sys

PYTHON = (3, 11)
GTK = (4, 18)
ADW = (1, 8)

# Typelib name -> what to install, in the common package names.
PACKAGES = {
    "gi": "PyGObject (python3-gi on Debian/Ubuntu, python3-gobject on Fedora/openSUSE, python-gobject on Arch, "
    "py3-gobject3 on Alpine)",
    "Gtk": "GTK 4 (gir1.2-gtk-4.0 on Debian/Ubuntu, gtk4 on Fedora/Arch, typelib-1_0-Gtk-4_0 on openSUSE, "
    "gtk4.0 on Alpine)",
    "Adw": "libadwaita (gir1.2-adw-1 on Debian/Ubuntu, libadwaita on Fedora/Arch/Alpine, typelib-1_0-Adw-1 on openSUSE)",
    "gir": "the GObject introspection data (gir1.2-freedesktop on Debian/Ubuntu, gobject-introspection on "
    "Fedora/Arch/Alpine, girepository-1_0 on openSUSE)",
    "Gst": "GStreamer (gir1.2-gstreamer-1.0 on Debian/Ubuntu, gstreamer1 on Fedora, gstreamer on Arch/Alpine, "
    "typelib-1_0-Gst-1_0 on openSUSE)",
}
GST_PLUGINS = (
    "the GStreamer base and good plugins (gstreamer1.0-plugins-base/-good on Debian/Ubuntu, "
    "gstreamer1-plugins-base/-good on Fedora, gst-plugins-base/-good on Arch/Alpine, "
    "gstreamer-plugins-base/-good on openSUSE)"
)
# pulsesrc moved into plugins-good on Debian/Ubuntu only recently.
PULSE_PLUGIN = "gstreamer1.0-pulseaudio on older Debian/Ubuntu releases"
GST_ELEMENTS = ("oggdemux", "opusdec")
# What playbin needs per file type: Cassini .opus files and recorded WAV tracks.
PLAYBACK_ELEMENTS = {".opus": GST_ELEMENTS, ".wav": ("wavparse",)}
# What Record Meeting needs (see recorder.capture_pipeline).
RECORDING_ELEMENTS = ("pulsesrc", "audiointerleave", "opusenc", "oggmux")

INSTALL = (
    "Install the missing packages with your distribution's package manager, or use the Flatpak, which bundles "
    "these libraries."
)
UPGRADE = "Upgrade your distribution to a release that has them, or use the Flatpak, which bundles these libraries."


class Outdated(str):
    """A problem that installing packages cannot fix: the distribution's version is too old."""


def version_text(version):
    return ".".join(str(part) for part in version)


def too_old(name, have, need):
    """Return a problem description if *have* is older than *need*, else None."""
    if tuple(have) >= tuple(need):
        return None
    return Outdated(f"{name} {version_text(need)} or newer is needed, but {version_text(have)} is installed")


def message(problems):
    """The text shown when the app cannot start."""
    advice = UPGRADE if all(isinstance(problem, Outdated) for problem in problems) else INSTALL
    return "Cassini cannot start: " + "; ".join(problems) + ". " + advice


def missing_gst_elements(find, names=GST_ELEMENTS):
    """Names from *names* for which *find(name)* returns None."""
    return [name for name in names if find(name) is None]


def playback_elements(uri):
    """The elements needed to play *uri*, judged by its extension."""
    for suffix, names in PLAYBACK_ELEMENTS.items():
        if uri.lower().endswith(suffix):
            return names
    return GST_ELEMENTS


def all_elements():
    """Every GStreamer element Cassini uses: playbin, each file type's decoders and Record Meeting's."""
    names = ["playbin"]
    for name in [name for names in PLAYBACK_ELEMENTS.values() for name in names] + list(RECORDING_ELEMENTS):
        if name not in names:
            names.append(name)
    return tuple(names)


def plugins_for(missing):
    """What to install for the *missing* GStreamer elements."""
    if "pulsesrc" in missing:
        return f"{GST_PLUGINS}, and {PULSE_PLUGIN}"
    return GST_PLUGINS


def gst_warning(missing):
    names = ", ".join(missing)
    return f"Cassini: missing GStreamer elements: {names}. Recordings will not play; install {GST_PLUGINS}."


def recording_warning(missing):
    names = ", ".join(missing)
    plugins = plugins_for(missing)
    return f"Cassini: missing GStreamer elements: {names}. Record Meeting will not work; install {plugins}."


def _problems():
    problems = []
    version = sys.version_info[:2]
    problem = too_old("Python", version, PYTHON)
    if problem:
        return [problem]
    try:
        import gi
    except ImportError:
        return ["the Python module 'gi' is missing; install " + PACKAGES["gi"]]

    for namespace, api in (("Gtk", "4.0"), ("Adw", "1"), ("Gst", "1.0")):
        try:
            gi.require_version(namespace, api)
        except ValueError:
            problems.append(f"the {namespace} {api} introspection data is missing; install {PACKAGES[namespace]}")
    if problems:
        return problems

    try:
        from gi.repository import Adw, Gst, Gtk
    except ImportError as exc:
        # Usually a typelib these depend on, such as cairo on a minimal Fedora.
        return [f"loading GTK, libadwaita and GStreamer failed ({exc}); install " + PACKAGES["gir"]]

    gtk = (Gtk.get_major_version(), Gtk.get_minor_version(), Gtk.get_micro_version())
    adw = (Adw.get_major_version(), Adw.get_minor_version(), Adw.get_micro_version())
    for name, have, need in (("GTK", gtk, GTK), ("libadwaita", adw, ADW)):
        problem = too_old(name, have, need)
        if problem:
            problems.append(problem)
    if problems:
        return problems

    Gst.init(None)
    if Gst.ElementFactory.find("playbin") is None:
        problems.append("the GStreamer element 'playbin' is missing; install " + GST_PLUGINS)
        return problems
    playback = [name for names in PLAYBACK_ELEMENTS.values() for name in names]
    missing = missing_gst_elements(Gst.ElementFactory.find, playback)
    if missing:
        print(gst_warning(missing), file=sys.stderr)
    missing = missing_gst_elements(Gst.ElementFactory.find, RECORDING_ELEMENTS)
    if missing:
        print(recording_warning(missing), file=sys.stderr)
    return problems


def check():
    """Exit with a readable message if a required library is missing or too old."""
    problems = _problems()
    if problems:
        print(message(problems), file=sys.stderr)
        sys.exit(1)
