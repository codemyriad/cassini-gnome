# SPDX-License-Identifier: GPL-3.0-or-later
"""Broker payload identity; load only on a worker, freeze once per service."""

import hashlib
import os
import time
from pathlib import Path

from .installed_build import read


def expected(root=None):
    root = Path(root) if root is not None else Path(__file__).resolve().parents[1]
    manifest = root / "build-info.json"
    if manifest.exists():
        build = read(manifest)
        version, identifier = build["version"], build["buildId"]
    else:
        # Source checkouts have no generated manifest. Include every runtime
        # module/catalog, never Git HEAD (several commits may share a payload).
        digest = hashlib.sha256()
        for path in sorted((root / "cassini_talk").rglob("*")):
            if path.is_file() and path.suffix in (".py", ".json"):
                digest.update(str(path.relative_to(root)).encode() + b"\0" + path.read_bytes() + b"\0")
        version, identifier = "source", digest.hexdigest()
    return dict(app="cassini-talk", version=version, buildId=identifier, moduleRoot=str(root.resolve()))


def target():
    local = expected()
    if os.environ.get("FLATPAK_ID") in {"io.codemyriad.CassiniTalk", "io.codemyriad.CassiniTalk.Devel"}:
        from .installed_build import host_identity

        build, _location = host_identity()
        local.update(version=build["version"], buildId=build["buildId"])
    return local


def running():
    return dict(expected(), pid=os.getpid(), startedAt=time.time_ns() // 1_000_000)


def matches(identity, local=None):
    local = target() if local is None else local
    return isinstance(identity, dict) and all(identity.get(key) == value for key, value in local.items())
