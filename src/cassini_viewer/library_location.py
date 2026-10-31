# SPDX-License-Identifier: GPL-3.0-or-later
"""GNOME-owned Library routing and durable D15.1 move journal; no GTK dependency."""

import contextlib
import fcntl
import json
import os
import tempfile
import threading
from functools import cache
from pathlib import Path
from uuid import uuid4

LIMIT = 64 * 1024 * 1024

_LOCAL_LOCK = threading.RLock()
_PRIMARY_CACHE = None


def config_path():
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "cassini/library.json"


def music_directory():
    try:
        from gi.repository import GLib
    except ImportError:
        return Path.home() / "Music"
    return Path(GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_MUSIC) or Path.home() / "Music")


@cache
def _sandbox_mounts():
    # Flatpak's root/home is a per-launch tmpfs. A filesystem permission or
    # the private app-data directory adds a bind mount that survives exit.
    # Mount namespaces do not change during this process's lifetime.
    def unescape(value):
        for encoded, decoded in (("\\040", " "), ("\\011", "\t"), ("\\012", "\n"), ("\\134", "\\")):
            value = value.replace(encoded, decoded)
        return Path(value)

    return tuple(
        unescape(line.split()[4])
        for line in Path("/proc/self/mountinfo").read_text().splitlines()
        if len(line.split()) >= 6 and line.split()[4] != "/"
    )


def _host_storage(path):
    resolved = Path(path).resolve()
    return any(mount == resolved or mount in resolved.parents for mount in _sandbox_mounts())


def initial_root():
    override = os.environ.get("CASSINI_LIBRARY_DIR")
    if override is not None:
        return Path(override).absolute()
    root = (music_directory() / "Cassini").absolute()
    from .companion import in_flatpak

    if in_flatpak() and not _host_storage(root):
        # No xdg-music mount (unconfigured/disabled/revoked): never record
        # into the sandbox's temporary home. App data is persistent without
        # granting access to any additional host files.
        root = Path(os.environ["XDG_DATA_HOME"]) / "cassini" / "Library"
        if not _host_storage(root):
            raise OSError("The Library has no persistent storage in this sandbox")
    return root


def load():
    try:
        with config_path().open("rb") as stream:
            raw = stream.read(LIMIT + 1)
    except FileNotFoundError:
        root = initial_root()
        value = dict(version=1, primary=str(root), pending=[], aliases={})
        from .companion import in_flatpak

        if in_flatpak() and root == Path(os.environ["XDG_DATA_HOME"]) / "cassini/Library":
            # Keep this choice after Music is configured later. Publish only
            # if still absent: another process may have moved the Library.
            save(value, only_if_missing=True)
            return load()
        return value
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
                elif event["state"] == "waiting" and source in pending:
                    pending[source].update({k: v for k, v in event.items() if k != "generation"})
                elif source in pending:
                    pending[source].update(state="failed", error=event["error"])
    except FileNotFoundError:
        pass
    value["pending"] = list(pending.values())
    return value


def save(value, *, only_if_missing=False):
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
        if only_if_missing:
            try:
                os.link(temporary, path)
            except FileExistsError:
                return
        else:
            os.replace(temporary, path)
        # The new generation makes any old progress records harmless even if
        # the process exits before this compaction finishes.
        if not only_if_missing:
            journal_fd = os.open(
                path.with_suffix(".journal"), os.O_CREAT | os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW, 0o600
            )
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


def _pair_checkpoint(entry, record):
    """The paired mover already holds routing before its Library locks."""
    value = load()
    current = next(
        (e for e in value["pending"] if (e["source"], e["target"]) == (entry["source"], entry["target"])), None
    )
    if current is None:
        from .library_move import MoveBusy

        raise MoveBusy("Library routing changed before the move committed")
    event = dict(current, pair=dict(record), state="waiting", generation=value["generation"])
    raw = (json.dumps(event, ensure_ascii=False) + "\n").encode()
    if len(raw) > 1024 * 1024:
        raise ValueError("Library move checkpoint exceeds limit")
    fd = os.open(config_path().with_suffix(".journal"), os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW)
    with os.fdopen(fd, "ab") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    entry["pair"] = dict(record)


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
        return Path(load()["primary"])
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
    from .library_publication import root_for

    value = value or load()
    return tuple(
        dict.fromkeys(
            [Path(value["primary"]), *(root_for(e["source"]) or Path(e["source"]).parent for e in value["pending"])]
        )
    )


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
    from .library_publication import root_for

    managed_root = root_for(original)
    for entry in value["pending"]:
        if entry["source"] == str(canonical) and entry.get("pair", {}).get("phase") in ("published", "retired"):
            return Path(entry["target"]).resolve()
    if canonical.parent in allowed or managed_root in allowed or canonical in pending and original.exists():
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
    if not original.is_symlink() and (
        canonical.parent in allowed or root_for(original) in allowed or canonical in pending
    ):
        return canonical
    raise ValueError("Meeting is outside the configured Library")


def forget_meeting(directory):
    """Discard only the removed folder's routing aliases and pending moves."""
    removed = {str(Path(directory).resolve())}
    with routing():
        value = load()
        before = json.dumps(value, sort_keys=True)
        while True:
            extra = {source for source, target in value["aliases"].items() if target in removed}
            extra.update(
                path
                for entry in value["pending"]
                if entry["source"] in removed or entry["target"] in removed
                for path in (entry["source"], entry["target"])
            )
            if extra <= removed:
                break
            removed.update(extra)
        value["pending"] = [
            entry for entry in value["pending"] if entry["source"] not in removed and entry["target"] not in removed
        ]
        value["aliases"] = {
            source: target
            for source, target in value["aliases"].items()
            if source not in removed and target not in removed
        }
        if json.dumps(value, sort_keys=True) != before:
            save(value)


def request(destination):
    from .library import meeting_id
    from .library_publication import capture_parent, root_for

    destination = Path(destination).resolve()
    with routing():
        value = load()
        if any(entry.get("pair") for entry in value["pending"]):
            raise ValueError("Finish the current Library move before choosing another destination")
        sources = {Path(e["source"]).resolve() for e in value["pending"]}
        for root in (Path(value["primary"]).resolve(),):
            if root.exists():
                from . import portable
                from .library_v2 import journal, xschema

                for file in root.iterdir():
                    if file.suffix != ".opus" or file.is_symlink() or not file.is_file():
                        continue
                    embedded = portable.read_meeting(str(file))
                    ours = (embedded.manifest or {}).get("x", {}).get(xschema.DOMAIN)
                    if not isinstance(ours, dict) or not xschema.valid(ours):
                        raise ValueError("Unowned Library files need the shared adoption API before moving")
                    sources.add(journal.Layout(root).session(ours["origin"], ours["sessionId"]))
                sources.update(p for p in root.iterdir() if meeting_id(p) and not p.is_symlink() and p.is_dir())
                sessions = root / ".cassini/sessions"
                if not (root / ".cassini").is_symlink() and not sessions.is_symlink() and sessions.is_dir():
                    sources.update(p for p in sessions.iterdir() if meeting_id(p) and not p.is_symlink() and p.is_dir())
        if any(p == destination or p in destination.parents for p in sources):
            raise ValueError("The destination cannot be inside a meeting")
        destination.mkdir(mode=0o700, parents=True, exist_ok=True)
        managed_parent = capture_parent(destination) if any(root_for(p) is not None for p in sources) else None
        value["primary"] = str(destination)
        value["pending"] = [
            dict(
                source=str(p),
                target=str((managed_parent if root_for(p) is not None else destination) / p.name),
                state="waiting",
            )
            for p in sorted(sources)
            if (root_for(p) or p.parent.resolve()) != destination
        ]
        save(value)
        return value


def process(*, busy=(), activity=None, retry=False, cancelled=None, changed=lambda: None, started=lambda: None):
    """Only one mover owns the routing journal; producers never take this lease."""
    path = config_path().with_suffix(".move.lock")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return load()
        return _process(
            busy=busy, activity=activity, retry=retry, cancelled=cancelled, changed=changed, started=started
        )
    finally:
        os.close(descriptor)


def _process(*, busy=(), activity=None, retry=False, cancelled=None, changed=lambda: None, started=lambda: None):
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
            from .library_publication import root_for

            if entry.get("pair") or root_for(source) is not None and read_metadata(source).get("published") is True:
                from .library_move_pair import move_pair

                started()
                move_pair(
                    source,
                    target,
                    record=entry.get("pair"),
                    checkpoint=lambda record, entry=entry: _pair_checkpoint(entry, record),
                    ready=ready,
                    cancelled=cancelled,
                )
            elif not source.exists() and target.is_dir() and not target.is_symlink():
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
        except FileNotFoundError as error:
            if not source.exists() and not target.exists() and source.parent.is_dir() and target.parent.is_dir():
                value["pending"].remove(entry)
                value["aliases"].pop(str(source), None)
            else:
                entry.update(state="failed", error=str(error))
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
            value = load()  # A producer/request may have published newer routing while copying.
            save(value)
    return value
