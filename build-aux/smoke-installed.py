#!/usr/bin/env python3
# Smoke-test an installed Cassini Viewer, for the package build scripts:
#   python3 -I build-aux/smoke-installed.py [datadir]
# datadir defaults to /usr/share/cassini-viewer. Runs the launcher's startup
# check, then fails unless every playback and recording GStreamer element is
# present: the app only warns about those, but a package must pull them in.
import sys

sys.path.insert(0, sys.argv[1] if len(sys.argv) > 1 else "/usr/share/cassini-viewer")
from cassini_viewer import runtime  # noqa: E402

runtime.check()  # exits with a readable message if a library is missing or too old
from gi.repository import Adw, Gst, Gtk  # noqa: E402

gtk = f"{Gtk.get_major_version()}.{Gtk.get_minor_version()}.{Gtk.get_micro_version()}"
adw = f"{Adw.get_major_version()}.{Adw.get_minor_version()}.{Adw.get_micro_version()}"
print(f"GTK {gtk}, libadwaita {adw}, {Gst.version_string()}")
missing = runtime.missing_gst_elements(Gst.ElementFactory.find, runtime.all_elements())
if missing:
    sys.exit("smoke test failed: missing GStreamer elements: " + ", ".join(missing))
print("smoke test passed: " + ", ".join(runtime.all_elements()))
