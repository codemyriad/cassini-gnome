#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""D16 build-time runtime acquisition. Meson invokes --offline; never fetch models."""

import argparse
import hashlib
import json
import shutil
import tempfile
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
PINS_FILE = HERE.parent / "src/cassini_viewer/sherpa-pins.json"
PINS = json.loads(PINS_FILE.read_text())
LINUX = PINS["sherpa-onnx-go-linux"]
FILES = {Path(name).name: (name, pin) for name, pin in LINUX["files"].items() if not name.endswith("cxx-api.so")}
BASE = "https://raw.githubusercontent.com/codemyriad/sherpa-onnx-go-linux/" + LINUX["commit"] + "/"
COPIED = ("LICENSE-sherpa-Apache-2.0.txt", "LICENSE-ONNXRuntime-MIT.txt", "NOTICE.md")


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def cached(cache, name, url, *, offline, expected=None):
    path = cache / name
    if not path.exists():
        if offline:
            raise FileNotFoundError(
                f"Missing pinned {name} in {cache}; run build-aux/sherpa-cache.sh before the offline build"
            )
        request = urllib.request.Request(
            url, headers={"Accept-Encoding": "identity", "User-Agent": "Cassini-build/0.2"}
        )
        with tempfile.NamedTemporaryFile(dir=cache, delete=False) as output:
            temporary = Path(output.name)
            try:
                with urllib.request.urlopen(request, timeout=90) as response:
                    shutil.copyfileobj(response, output, 128 * 1024)
                output.flush()
                if expected and (temporary.stat().st_size != expected["size"] or sha(temporary) != expected["sha256"]):
                    raise ValueError("Pinned runtime differs: " + name)
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
    if expected and (path.stat().st_size != expected["size"] or sha(path) != expected["sha256"]):
        raise ValueError("Pinned runtime differs: " + name)
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", nargs="?", type=Path)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--verify", type=Path)
    args = parser.parse_args()
    if args.verify:
        manifest = json.loads((args.verify / "manifest.json").read_text())
        expected = {name: pin["sha256"] for name, (_path, pin) in FILES.items()}
        expected.update({name: sha(HERE / name) for name in COPIED})
        expected["pins.json"] = sha(PINS_FILE)
        if manifest != expected or {p.name for p in args.verify.iterdir()} != set(expected) | {"manifest.json"}:
            raise ValueError("Unexpected runtime bundle files or manifest")
        for name, digest in manifest.items():
            if sha(args.verify / name) != digest:
                raise ValueError("Runtime bundle hash mismatch: " + name)
        print("Verified D16 runtime bundle:", args.verify)
        return
    args.cache.mkdir(parents=True, exist_ok=True)
    sums_file = cached(args.cache, "CASSINI-SHA256SUMS", BASE + "CASSINI-SHA256SUMS", offline=args.offline)
    sums = dict(
        (path.lstrip("*"), digest)
        for digest, path in (line.split(maxsplit=1) for line in sums_file.read_text().splitlines() if line.strip())
    )
    paths = {}
    for name, (upstream_path, expected) in FILES.items():
        if sums.get(upstream_path) != expected["sha256"]:
            raise ValueError("Fork CASSINI-SHA256SUMS differs from D16: " + name)
        paths[name] = cached(args.cache, name, BASE + upstream_path, offline=args.offline, expected=expected)
    if args.output:
        args.output.mkdir(parents=True, exist_ok=True)
        for name, path in paths.items():
            shutil.copyfile(path, args.output / name)
        for name in COPIED:
            shutil.copyfile(HERE / name, args.output / name)
        shutil.copyfile(PINS_FILE, args.output / "pins.json")
        manifest = {path.name: sha(path) for path in args.output.iterdir() if path.name != "manifest.json"}
        (args.output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print("Verified two pinned libraries; no C++ wrapper or model downloaded")


if __name__ == "__main__":
    main()
