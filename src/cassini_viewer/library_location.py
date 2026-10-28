# SPDX-License-Identifier: GPL-3.0-or-later
"""GNOME-owned Library routing and durable D15.1 move journal; no GTK dependency."""

import contextlib
import fcntl
import json
import os
import tempfile
import threading
from pathlib import Path
from uuid import uuid4

LIMIT = 64 * 1024 * 1024

_LOCAL_LOCK = threading.RLock()
_PRIMARY_CACHE = None


def config_path():
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "cassini/library.json"


def initial_root():
    return Path(os.environ.get("CASSINI_LIBRARY_DIR", Path.home() / "Music/Cassini")).absolute()


def load():
    try:
        with config_path().open("rb") as stream:
            raw = stream.read(LIMIT + 1)
    except FileNotFoundError:
        return dict(version=1, primary=str(initial_root()), pending=[], aliases={})
    value = json.loads(raw)
    if len(raw) > LIMIT or value.get("version") != 1:
        raise ValueError("Invalid Library location configuration")
    paths = [value["primary"]]
    for entry in value["pending"]:
        paths.extend((entry["source"], entry["target"]))
        if entry["state"] not in ("waiting", "failed"):
            raise ValueError("Invalid Library move state")
    paths.extend(value.get("aliases", {}).keys())
    paths.extend(value.get("aliases", {}).values())
    if not all(isinstance(p, str) and Path(p).is_absolute() for p in paths):
        raise ValueError("Library paths must be absolute")
    value.setdefault("aliases", {})
    pending = {e["source"]: e for e in value["pending"]}
    try:
        with config_path().with_suffix(".journal").open("rb") as stream:
            for line in stream:
                if len(line) > 1024 * 1024:
                    raise ValueError("Library journal entry exceeds limit")
                if not line.endswith(b"\n"):
                    break  # A crash may leave the final append incomplete.
                event = json.loads(line)
                if event.get("generation") != value.get("generation"):
                    continue
                source = event["source"]
                if event["state"] == "done":
                    pending.pop(source, None)
                    value["aliases"][source] = event["target"]
                elif source in pending:
                    pending[source].update(state="failed", error=event["error"])
    except FileNotFoundError:
        pass
    value["pending"] = list(pending.values())
    return value


def save(value):
    value["generation"] = uuid4().hex
    path = config_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".library-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as output:
            json.dump(value, output, ensure_ascii=False, allow_nan=False)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        # The new generation makes any old progress records harmless even if
        # the process exits before this compaction finishes.
        journal_fd = os.open(path.with_suffix(".journal"), os.O_CREAT | os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        try:
            os.fsync(journal_fd)
        finally:
            os.close(journal_fd)
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _append_progress(value, entry, done):
    event = dict(entry, generation=value["generation"], state="done" if done else "failed")
    raw = (json.dumps(event, ensure_ascii=False) + "\n").encode()
    fd = os.open(config_path().with_suffix(".journal"), os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW)
    with os.fdopen(fd, "ab") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


@contextlib.contextmanager
def routing():
    """Serialize destination publication with default producer-folder creation."""
    with _LOCAL_LOCK:
        path = config_path().with_suffix(".lock")
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)


def capture_lease(identifier):
    path = config_path().parent / "captures" / (identifier + ".lock")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return fd


def capture_busy(identifier):
    path = config_path().parent / "captures" / (identifier + ".lock")
    try:
        fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
    except FileNotFoundError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        return False
    finally:
        os.close(fd)


def primary():
    # Routing is used from GTK too. Read only the small leading path value,
    # never the full pending/alias journal for a caption or a Record action.
    global _PRIMARY_CACHE
    path = config_path()
    try:
        info = path.stat()
    except FileNotFoundError:
        return initial_root()
    signature = (str(path), info.st_ino, info.st_mtime_ns, info.st_size)
    if _PRIMARY_CACHE and _PRIMARY_CACHE[0] == signature:
        return _PRIMARY_CACHE[1]
    with path.open("rb") as stream:
        head = stream.read(32768).decode("utf-8", "ignore")
    prefix = '{"version": 1, "primary": '
    if head.startswith(prefix):
        value, _end = json.JSONDecoder().raw_decode(head, len(prefix))
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise ValueError("Invalid Library primary path")
    else:
        value = load()["primary"]  # Accept reordered older/hand-edited configurations.
    result = Path(value)
    _PRIMARY_CACHE = signature, result
    return result


def roots(value=None):
    value = value or load()
    return tuple(dict.fromkeys([Path(value["primary"]), *(Path(e["source"]).parent for e in value["pending"])]))


def owned_directory(path):
    """Retained busy/failed folders remain editable; unrelated old-root folders do not."""
    from .library import meeting_id

    original = Path(path).absolute()
    if original.is_symlink() or not meeting_id(original):
        raise ValueError("Meeting is outside the configured Library")
    value = load()
    allowed = {Path(value["primary"]).resolve()}
    pending = {Path(e["source"]).resolve() for e in value["pending"]}
    canonical = original.resolve()
    if canonical.parent in allowed or canonical in pending and original.exists():
        return canonical
    for entry in value["pending"]:
        if not original.exists() and Path(entry["source"]).resolve() == canonical:
            target = Path(entry["target"])
            if target.is_dir() and not target.is_symlink():
                return target.resolve()
    original = canonical
    visited = set()
    while not original.exists() and str(original) in value["aliases"] and str(original) not in visited:
        visited.add(str(original))
        original = Path(value["aliases"][str(original)])
    canonical = original.resolve()
    if not original.is_symlink() and (canonical.parent in allowed or canonical in pending):
        return canonical
    raise ValueError("Meeting is outside the configured Library")


def request(destination):
    from .library import meeting_id

    destination = Path(destination).resolve()
    with routing():
        value = load()
        sources = {Path(e["source"]).resolve() for e in value["pending"]}
        for root in (Path(value["primary"]).resolve(),):
            if root.exists():
                sources.update(p for p in root.iterdir() if meeting_id(p) and not p.is_symlink() and p.is_dir())
        if any(p == destination or p in destination.parents for p in sources):
            raise ValueError("The destination cannot be inside a meeting")
        destination.mkdir(mode=0o700, parents=True, exist_ok=True)
        value["primary"] = str(destination)
        value["pending"] = [
            dict(source=str(p), target=str(destination / p.name), state="waiting")
            for p in sorted(sources)
            if p.parent.resolve() != destination
        ]
        save(value)
        return value


def process(*, busy=(), activity=None, retry=False, cancelled=None, changed=lambda: None, started=lambda: None):
    """Finish idle folders, checkpointing each. Never automatically retry failures."""
    from concurrent.futures import CancelledError

    from .library import meeting_id
    from .library_move import MoveBusy, move_folder
    from .meeting_store import LOCK, read_metadata

    activity = activity or {}
    busy = {Path(p).resolve() for p in busy}
    with routing():
        value = load()
        journal = config_path().with_suffix(".journal")
        if journal.exists() and journal.stat().st_size:
            save(value)
        retried = {entry["source"] for entry in value["pending"] if entry["state"] == "failed"}
        if retry:
            for entry in value["pending"]:
                if entry["state"] == "failed":
                    entry["state"] = "waiting"
            save(value)
    progressed = False
    for entry in list(value["pending"]):
        if cancelled is not None and cancelled.is_set():
            break
        if entry["state"] == "failed" or retry and entry["source"] not in retried:
            continue
        source, target = Path(entry["source"]), Path(entry["target"])

        def ready(source=source):
            if (
                source.resolve() in busy
                or activity.get(meeting_id(source)) in ("recording", "saving")
                or capture_busy(meeting_id(source))
            ):
                return False
            try:
                return read_metadata(source)["transcript"]["status"] not in ("queued", "running")
            except (OSError, ValueError):
                return True  # Opaque/unreadable folders can still be renamed intact.

        try:
            if not source.exists() and target.is_dir() and not target.is_symlink():
                pass  # Recover an already-published complete folder.
            else:
                with LOCK:
                    if not ready():
                        continue
                started()
                move_folder(source, target.parent, cancelled=cancelled, ready=ready)
            value["aliases"][str(source)] = str(target)
            value["pending"].remove(entry)
        except MoveBusy:
            continue
        except CancelledError:
            break
        except (OSError, ValueError) as error:
            entry.update(state="failed", error=str(error))
        with routing():
            _append_progress(value, entry, entry not in value["pending"])
        progressed = True
        changed()
    if progressed:
        with routing():
            save(value)
    return value
