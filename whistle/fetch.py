#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Build-time fetch only; pinned downloads and installed files fail closed.

  fetch.py --download-only --cache DIR     fill the cache, nothing else
  fetch.py OUT --cache DIR [--offline]     build the verified bundle in OUT
  fetch.py --onnxruntime cpXY OUT --cache DIR [--offline]
                                           unpack the pinned ONNX Runtime
                                           wheel for CPython X.Y into OUT

Package builds fill the cache first (build-aux/whistle-cache.sh or their own
pinned sources) and then build with --offline, which never downloads.
"""

import argparse
import hashlib
import http.client
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ATTEMPTS = 3
# The bridge is linked already stripped, with the usual hardening flags, so
# its hash in manifest.json is final: packaging must never strip the bundle.
BRIDGE_FLAGS = ["-shared", "-fPIC", "-O2", "-Wall", "-Wextra", "-Werror", "-s", "-Wl,-z,relro,-z,now"]
COPIED = ("pins.json", "NOTICE.md", "LICENSE-Apache-2.0.txt", "LICENSE-Silero-MIT.txt", "LICENSE-ONNXRuntime-MIT.txt")
# The app needs only ONNX Runtime's Python API and its CPU extension module,
# which links the runtime statically: not the model-conversion tools and the
# ONNX backend adapter (which import sympy, onnx, packaging and others), nor
# the separate C API library.
ONNXRUNTIME_DROPPED = (
    "onnxruntime/transformers/",
    "onnxruntime/quantization/",
    "onnxruntime/tools/",
    "onnxruntime/backend/",
    "onnxruntime/capi/libonnxruntime.so.",
)


def verify(path, expected):
    if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
        raise ValueError(f"SHA256 mismatch: {path.name}")


def download(name, info, target):
    request = urllib.request.Request(info["url"], headers={"Accept-Encoding": "identity"})
    for attempt in range(1, ATTEMPTS + 1):
        with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as staged:
            temporary = Path(staged.name)
            try:
                with urllib.request.urlopen(request, timeout=90) as response:
                    shutil.copyfileobj(response, staged)
                staged.flush()
                # A hash mismatch is never retried: the pin or the upstream is wrong.
                verify(temporary, info["sha256"])
                temporary.chmod(0o644)
                temporary.replace(target)
                return
            except (OSError, http.client.HTTPException) as error:
                if attempt == ATTEMPTS:
                    raise RuntimeError(f"Could not download {name} from {info['url']}: {error}") from error
                print(f"{name}: attempt {attempt} failed ({error}); retrying", file=sys.stderr)
            finally:
                temporary.unlink(missing_ok=True)
        time.sleep(5 * attempt)


def cached(cache, name, info, *, offline):
    target = cache / name
    if not target.exists():
        if offline:
            raise FileNotFoundError(
                f"{name} is missing from {cache} and this build is offline. "
                f"Put {info['url']} there as {name} (sha256 {info['sha256']}), "
                "or run build-aux/whistle-cache.sh."
            )
        download(name, info, target)
    verify(target, info["sha256"])
    return target


def wheel_name(info):
    return info["url"].rsplit("/", 1)[1]


def fill_cache(cache, *, offline, wheels=False):
    """The bundle's artifacts; with wheels, also every pinned ONNX Runtime wheel."""
    pins = json.loads((HERE / "pins.json").read_text())
    if not offline:
        cache.mkdir(parents=True, exist_ok=True)
    for name, info in pins["artifacts"].items():
        cached(cache, name, info, offline=offline)
    if wheels:
        for info in pins["onnxruntime"]["wheels"].values():
            cached(cache, wheel_name(info), info, offline=offline)
    return pins


def build_onnxruntime(out, cache, abi, *, offline=False):
    """Unpack the pinned ONNX Runtime wheel for CPython ABI abi (cp313...) into
    out, a private directory the recognition child puts first on sys.path.
    The dist-info RECORD stays, so verify.py --onnxruntime (and the child,
    before it loads anything) can check every installed file against it."""
    pins = json.loads((HERE / "pins.json").read_text())["onnxruntime"]
    if abi not in pins["wheels"]:
        raise ValueError(f"No pinned ONNX Runtime wheel for {abi}; pins.json has {', '.join(pins['wheels'])}")
    info = pins["wheels"][abi]
    wheel = cached(cache, wheel_name(info), info, offline=offline)
    top = ("onnxruntime/", f"onnxruntime-{pins['version']}.dist-info/")
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=out.parent) as stage:
        stage = Path(stage)
        with zipfile.ZipFile(wheel) as archive:
            if archive.read("onnxruntime/LICENSE") != (HERE / "LICENSE-ONNXRuntime-MIT.txt").read_bytes():
                raise ValueError("The ONNX Runtime wheel's LICENSE differs from LICENSE-ONNXRuntime-MIT.txt")
            for member in archive.infolist():
                name = member.filename
                if member.is_dir() or name.startswith(ONNXRUNTIME_DROPPED):
                    continue
                if not name.startswith(top) or ".." in name.split("/") or "\\" in name:
                    raise ValueError(f"Unexpected file in the ONNX Runtime wheel: {name}")
                target = stage / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(member))
                target.chmod(0o644)
        for directory in (stage, *(path for path in stage.rglob("*") if path.is_dir())):
            directory.chmod(0o755)
        onnxruntime_verify(stage)
        if out.exists():
            shutil.rmtree(out)
        stage.rename(out)


def onnxruntime_verify(path):
    sys.path.insert(0, str(HERE.parent / "src"))
    from cassini_viewer.whistle_worker import verify_onnxruntime

    return verify_onnxruntime(path)


def fetch(out, cache, *, offline=False):
    pins = fill_cache(cache, offline=offline)
    if out.exists():
        if json.loads((out / "pins.json").read_text()) != pins:
            raise ValueError("Existing bundle pins differ")
        manifest = json.loads((out / "manifest.json").read_text())
        if set(manifest) != {*pins["installed"], "libcassini_whistle_cpu.so"}:
            raise ValueError("Existing bundle manifest differs")
        for name, expected in pins["installed"].items():
            if manifest[name] != expected:
                raise ValueError("Existing bundle hashes differ")
        for name, expected in manifest.items():
            verify(out / name, expected)
        for name in ("cpu_limit.c", *COPIED[1:]):
            if (out / name).read_bytes() != (HERE / name).read_bytes():
                raise ValueError(f"Existing bundle source/license differs: {name}")
        return
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=out.parent) as stage:
        stage = Path(stage)
        for name in ("whistle.cact", "wrapper.whl", "silero_vad.onnx"):
            shutil.copyfile(cache / name, stage / name)
        with zipfile.ZipFile(cache / "engine.whl") as archive:
            (stage / "libneedle3.so").write_bytes(archive.read("needle/libneedle3.so"))
        for name, expected in pins["installed"].items():
            verify(stage / name, expected)
        for name in COPIED:
            shutil.copyfile(HERE / name, stage / name)
        subprocess.run(
            shlex.split(os.environ.get("CC", "cc"))
            + BRIDGE_FLAGS
            + [str(HERE / "cpu_limit.c"), "-o", str(stage / "libcassini_whistle_cpu.so"), "-ldl"],
            check=True,
        )
        manifest = dict(pins["installed"])
        manifest["libcassini_whistle_cpu.so"] = hashlib.sha256(
            (stage / "libcassini_whistle_cpu.so").read_bytes()
        ).hexdigest()
        (stage / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        shutil.copyfile(HERE / "cpu_limit.c", stage / "cpu_limit.c")
        for path in stage.iterdir():
            path.chmod(0o644)
        # TemporaryDirectory is 0700; the installed bundle must be readable.
        stage.chmod(0o755)
        stage.rename(out)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("out", type=Path, nargs="?")
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--onnxruntime", metavar="ABI", help="unpack the ONNX Runtime wheel for this CPython ABI")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--offline", action="store_true", help="never download; fail if the cache is incomplete")
    mode.add_argument("--download-only", action="store_true", help="only fill and verify the cache")
    args = parser.parse_args()
    try:
        if args.download_only:
            if args.out is not None or args.onnxruntime:
                parser.error("--download-only takes no output directory")
            fill_cache(args.cache, offline=False, wheels=True)
        elif args.out is None:
            parser.error("an output directory is required unless --download-only is given")
        elif args.onnxruntime:
            build_onnxruntime(args.out, args.cache, args.onnxruntime, offline=args.offline)
        else:
            fetch(args.out, args.cache, offline=args.offline)
    except (OSError, ValueError, RuntimeError) as error:
        sys.exit(f"whistle/fetch.py: {error}")
