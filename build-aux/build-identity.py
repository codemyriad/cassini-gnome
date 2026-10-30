#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Build a deterministic identity for the installed application payload."""

import argparse
import hashlib
import json
from pathlib import Path

EXCLUDED = {"native_host.py", "control_host.py", "install-host.py"}


def extension_files(root):
    return sorted(
        path
        for path in Path(root).rglob("*")
        if path.is_file() and not any(p in {"tests", "__pycache__"} for p in path.parts) and path.name not in EXCLUDED
    )


def generate(source, build, inputs, version, profile, application):
    source, build = Path(source).resolve(), Path(build).resolve()
    files = {}
    launcher = None
    for path in map(Path, inputs):
        path = path.resolve()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if path in (build / "src/cassini-talk", build / "src/cassini-viewer"):
            launcher = digest
        elif path == build / "src/paths.json":
            files["cassini_viewer/paths.json"] = digest
        elif path == build / "src/cassini-viewer.gresource":
            files[path.name] = digest
        elif path.is_relative_to(source / "src"):
            files[path.relative_to(source / "src").as_posix()] = digest
        elif path.is_relative_to(source / "browser-extension"):
            files[path.relative_to(source).as_posix()] = digest
        elif path.is_relative_to(source / "whistle"):
            # These build inputs are also copied verbatim into the bundle.
            # Engine-only patches must change the application identity too.
            files[path.relative_to(source).as_posix()] = digest
        else:
            raise ValueError(f"Unexpected identity input: {path}")
    if not files or launcher is None:
        raise ValueError("Incomplete application identity inputs")
    value = dict(schema=1, version=version, profile=profile, applicationId=application, launcher=launcher, files=files)
    value["buildId"] = hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return value


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list-extension", type=Path)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--build", type=Path)
    parser.add_argument("--version")
    parser.add_argument("--profile")
    parser.add_argument("--application")
    parser.add_argument("--output", type=Path)
    parser.add_argument("inputs", nargs="*")
    args = parser.parse_args()
    if args.list_extension:
        print("\n".join(str(p) for p in extension_files(args.list_extension)))
    else:
        result = generate(args.source, args.build, args.inputs, args.version, args.profile, args.application)
        args.output.write_text(json.dumps(result, sort_keys=True, indent=2) + "\n")
