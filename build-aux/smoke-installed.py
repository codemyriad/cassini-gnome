#!/usr/bin/env python3
# Smoke-test an installed Cassini talk, for the package build scripts:
#   python3 -I build-aux/smoke-installed.py [datadir] [--whistle GOLDENS]
# datadir defaults to /usr/share/cassini-talk. Runs the launcher's startup
# check, then fails unless every playback and recording GStreamer element is
# present: the app only warns about those, but a package must pull them in.
#
# --whistle GOLDENS (a copy of tests/whistle) also writes a meeting down the
# way the installed app does: it checks the installed bundle's hashes, that
# the package's private ONNX Runtime matches this Python and its wheel's
# RECORD, and that the GTK process cannot import ONNX Runtime (the package
# must not pull in the distribution's), then runs the installed worker
# on both D11 goldens (tests/whistle/pipeline_probe.py): segments and word
# texts exact, word times within 80 ms (D11.3). It refuses to run unless the
# only network interface is loopback, so run it in a container started with
# --network none (or flatpak run --unshare=network).
import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("datadir", nargs="?", default="/usr/share/cassini-talk", type=Path)
parser.add_argument("--whistle", type=Path, metavar="GOLDENS", help="also run the offline D11 goldens")
parser.add_argument("--parakeet", type=Path, metavar="MODEL", help="also run D16 using cached fixture weights")
parser.add_argument("--parakeet-tests", type=Path, metavar="TESTS", help="path to tests/parakeet")
args = parser.parse_args()

# D45.2/D51: every package must be a fresh current-name installation, not a
# compatibility layout. This check also runs inside the installed Flatpak.
prefix = args.datadir.parents[1]
required = [prefix / "bin/cassini-talk", args.datadir / "cassini_talk/__init__.py",
            args.datadir / "cassini-talk.gresource",
            prefix / "share/glib-2.0/schemas/io.codemyriad.CassiniTalk.gschema.xml"]
obsolete = [prefix / "bin/cassini-viewer", prefix / "share/cassini-viewer",
            args.datadir / "cassini_viewer",
            prefix / "share/glib-2.0/schemas/com.gocassini.Viewer.gschema.xml"]
if any(not path.is_file() for path in required) or any(path.exists() or path.is_symlink() for path in obsolete):
    sys.exit("clean Cassini talk installation layout does not match the package")
print("clean Cassini talk command, Python package, resource and schema verified")

sys.path.insert(0, str(args.datadir))
from cassini_talk import runtime  # noqa: E402

runtime.check()  # exits with a readable message if a library is missing or too old
from cassini_talk.installed_build import read, verified  # noqa: E402

identity = read(args.datadir / "build-info.json")
if not verified(identity, args.datadir, args.datadir.parents[1] / "bin/cassini-talk"):
    sys.exit("installed build identity does not match the package payload")
print("installed build identity verified: " + identity["buildId"])
from gi.repository import Adw, Gst, Gtk  # noqa: E402

gtk = f"{Gtk.get_major_version()}.{Gtk.get_minor_version()}.{Gtk.get_micro_version()}"
adw = f"{Adw.get_major_version()}.{Adw.get_minor_version()}.{Adw.get_micro_version()}"
print(f"GTK {gtk}, libadwaita {adw}, {Gst.version_string()}")
missing = runtime.missing_gst_elements(Gst.ElementFactory.find, runtime.all_elements())
if missing:
    sys.exit("smoke test failed: missing GStreamer elements: " + ", ".join(missing))
print("smoke test passed: " + ", ".join(runtime.all_elements()))

# D18.2 needs a read-only Pulse metadata query in every native/Flatpak package.
# --version proves the installed tool runs without contacting any audio server.
try:
    subprocess.run(["pactl", "--version"], check=True, capture_output=True, text=True, timeout=3)
except (OSError, subprocess.SubprocessError) as error:
    sys.exit(f"muted-output observation smoke test failed: pactl is unavailable: {error}")
print("muted-output observation query tool available")


# ModelStore uses ctypes, so ELF dependency scanners cannot discover libzstd.
# Exercise the installed app's loader in the same fresh/offline package smoke
# used for both recognition engines, including the Flatpak runtime.
from cassini_talk.zstd_stream import library as zstd_library  # noqa: E402

zstd = zstd_library()
stream = zstd.ZSTD_createDStream()
if not stream:
    sys.exit("model unpack smoke test failed: could not create a Zstandard stream")
try:
    if zstd.ZSTD_isError(zstd.ZSTD_initDStream(stream)):
        sys.exit("model unpack smoke test failed: could not initialise libzstd")
finally:
    zstd.ZSTD_freeDStream(stream)
print("model unpack runtime smoke test passed: libzstd")

if args.whistle:
    import importlib.util  # noqa: E402

    from cassini_talk.transcription import unavailable_reason  # noqa: E402
    from cassini_talk.whistle_worker import onnxruntime_dir, verify_bundle, verify_onnxruntime  # noqa: E402

    bundle = args.datadir / "whistle"
    verify_bundle(bundle)  # every hash in manifest.json: nothing stripped or moved
    runtime = onnxruntime_dir()
    if runtime is None:
        sys.exit("writing-down smoke test failed: no private ONNX Runtime for this Python")
    verify_onnxruntime(runtime)  # every file against the wheel's RECORD
    if importlib.util.find_spec("onnxruntime") is not None:
        sys.exit("writing-down smoke test failed: this Python has its own onnxruntime; the package must not need it")
    reason = unavailable_reason(bundle)
    if reason:
        sys.exit(f"writing-down smoke test failed: {reason}")
    interfaces = {name for _, name in socket.if_nameindex()}
    if interfaces != {"lo"}:
        sys.exit(f"writing-down smoke test must run without a network, but this has {sorted(interfaces)}")
    env = dict(os.environ, CASSINI_WHISTLE_NETWORK_NONE="1", PYTHONDONTWRITEBYTECODE="1")
    with tempfile.TemporaryDirectory() as temporary:
        report = Path(temporary) / "golden.json"
        probe = subprocess.run(
            [sys.executable, str(args.whistle / "pipeline_probe.py"), str(bundle), str(report)]
            + ["--modules", str(args.datadir)],
            env=env,
            capture_output=True,
            text=True,
            cwd=temporary,
        )
        if probe.returncode != 0:
            print(probe.stdout[-20000:], probe.stderr, sep="\n", file=sys.stderr)
            sys.exit("writing-down smoke test failed: the D11 goldens did not match")
        result = json.loads(report.read_text())
    import numpy

    pinned = json.loads((bundle / "pins.json").read_text())["onnxruntime"]["version"]
    if result["vad"]["version"] != pinned:
        sys.exit(f"writing-down smoke test failed: the child ran ONNX Runtime {result['vad']['version']}, not {pinned}")
    print(f"NumPy {numpy.__version__}, private ONNX Runtime {pinned} in {runtime}, {result['vad']['providers']}")
    for case in result["cases"]:
        print(
            f"{case['golden']}: {case['segments']} segments exact, {case['words']} words, "
            f"text exact {case['wordTextMatches']}, max word time difference {case['maxBoundaryDeltaMs']} ms"
        )
    print("writing-down smoke test passed offline (D11.3)")

if args.parakeet:
    from cassini_talk.sherpa_runtime import runtime_path, verify_runtime

    if not args.parakeet_tests:
        parser.error("--parakeet requires --parakeet-tests")
    verify_runtime(runtime_path())
    # No model download, no distro engine, and no fallback to a source build.
    with tempfile.TemporaryDirectory() as temporary:
        subprocess.run(
            [
                "bash",
                str(args.parakeet_tests / "run.sh"),
                "--installed",
                str(args.datadir),
                str(args.parakeet),
                temporary,
            ],
            check=True,
        )
    print("Parakeet package smoke passed offline: two goldens and two real FIFO jobs")
