# SPDX-License-Identifier: GPL-3.0-or-later
"""Broker payload identity; load only on a worker, freeze once per service."""

import hashlib
import os
import sys
import threading
import time
from concurrent.futures import Future
from pathlib import Path

from .installed_build import file_digest, read


def expected(root=None):
    root = Path(root) if root is not None else Path(__file__).resolve().parents[1]
    manifest = root / "build-info.json"
    if manifest.exists():
        build = read(manifest)
        if not all(file_digest(root / name) == digest for name, digest in build["files"].items()):
            raise OSError("Application update is still being installed")
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


_host_targets = {}
_host_lock = threading.Lock()


def _key(local):
    return (os.environ.get("FLATPAK_ID"), *(local[key] for key in ("app", "version", "buildId", "moduleRoot")))


def remember_host_identity(build):
    """Publish a freshly verified installed deployment from BuildRefresh's worker."""
    if os.environ.get("FLATPAK_ID") not in {"io.codemyriad.CassiniTalk", "io.codemyriad.CassiniTalk.Devel"}:
        return
    local = expected()
    future = Future()
    future.set_result((build, None))
    with _host_lock:
        _host_targets[_key(local)] = future


def target(*, wait=True):
    local = expected()
    if os.environ.get("FLATPAK_ID") not in {"io.codemyriad.CassiniTalk", "io.codemyriad.CassiniTalk.Devel"}:
        return local
    key = _key(local)
    with _host_lock:
        future = _host_targets.get(key)
        if future is None:
            future = _host_targets[key] = Future()
            start = True
        else:
            start = False
    if start:

        def probe():
            from .installed_build import host_identity

            try:
                future.set_result(host_identity())
            except Exception as error:
                future.set_exception(error)

        threading.Thread(target=probe, name="cassini-host-identity", daemon=True).start()
    # Diagnostic lookup must never wait on GTK, including the first call.
    glib = sys.modules.get("gi.repository.GLib")
    if not future.done() and (not wait or glib is not None and glib.MainContext.default().is_owner()):
        return local
    try:
        discovered = future.result()
    except (OSError, ValueError):
        discovered = None
    with _host_lock:
        latest = _host_targets.get(key)
    if latest is not None and latest is not future:
        discovered = latest.result()  # A verified refresh superseded this in-flight probe.
    if discovered is not None:
        build, _location = discovered
        local.update(version=build["version"], buildId=build["buildId"])
    return local


def running():
    return dict(expected(), pid=os.getpid(), startedAt=time.time_ns() // 1_000_000)


def matches(identity, local=None):
    try:
        local = target(wait=False) if local is None else local
    except (OSError, ValueError):
        return False
    return isinstance(identity, dict) and all(identity.get(key) == value for key, value in local.items())
