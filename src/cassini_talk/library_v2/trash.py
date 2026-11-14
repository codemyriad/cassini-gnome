# SPDX-License-Identifier: GPL-3.0-or-later
"""S-12 §7.1 (D100, D-1032): deleted meetings in `<Library>/.cassini/trash/<entry-id>/`.

A port of the cassini-ux reference model `contract/library-v2/trash.py` onto real files;
its shared vectors (`tests/ux/library-v2/trash-vectors.json`, every case and every crash
point) are replayed against this module by tests/test_library_v2_trash_vectors.py.

A meeting has one or two representations, found by identity (S-12 §1), never by title:
(a) a published root `.opus` only, (b) a published root `.opus` plus its working folder
`.cassini/sessions/<Recording|Received>-<uuid>/`, or (c) a working folder only (an
unpublished v2 session, or a v1 root meeting folder). Move to Trash, Restore and purge
(Delete permanently, Empty Trash, 30-day expiry) each run under `library.lock`, are
planned into a list of atomic steps, journaled durably first (`.cassini/journal/<op>.json`,
kinds `trash`, `restore`, `purge`), then applied in order. Every move is one no-replace
rename on the Library's volume (never the system Trash or the Trash portal, so it works in
the Flatpak); both directories are fsynced after each rename. Startup recovery replays
every pending entry FORWARD, step by step and idempotently:

  move   source present, target absent -> rename now; source gone, target present ->
         already done; both present -> conflict (stop this op, keep both copies and the
         journal entry, report, never delete; a restore's journaled name taken by a file
         placed there outside the app takes the next suffix instead and the journal entry
         is rewritten); neither present -> skipped.

Afterwards `.purge-*` directories without a pending entry are removed, and an entry
directory without `entry.json` and without a pending entry gets one rebuilt from its
contents. No byte of a meeting is rewritten (D51).

CHECKPOINT is a test hook called after each applied step (1-based count), so the crash
matrix can stop an operation after any step.
"""

import contextlib
import errno
import json
import os
import re
import shutil
import stat
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from . import fsutil, journal, names

JOURNAL = ".cassini/journal"
SESSIONS = ".cassini/sessions"
TRASH = ".cassini/trash"
ENTRY_FILE = "entry.json"
PUBLISHED_IN_ENTRY = "meeting.opus"
PURGE_PREFIX = ".purge-"
RETENTION_DAYS = 30
MAX_SUFFIX_RETRIES = 99
KINDS = ("trash", "restore", "purge")
STEP_KINDS = ("mkdir", "writeFile", "move", "remove", "removeTree")
SESSION_NAME = re.compile(r"(Recording|Received)-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
MAX_ENTRY_FILE = 1 << 16
CHECKPOINT = None


class Refused(Exception):
    """A precondition failed under the lock: nothing was journaled and nothing changed."""


class Conflict(Exception):
    """Replay found both a move's source and target: the operation stops, both copies stay."""


class Failed(OSError):
    """The operation stopped after it had changed something; its journal entry stays, and
    recovery finishes it forward."""


class Moved(OSError):
    """The rename itself happened, then making it durable (a directory fsync) failed: the
    meeting has moved, so the operation must be finished forward, never rolled back."""


def dumps(value):
    """The exact serialization of entry.json (the reference model's)."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _now_text(now=None):
    value = now or datetime.now(UTC)
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc(value):
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def days_left(deleted_at_utc, now_utc):
    """(daysLeft, expired): floor((deletedAt + 30 d - now) / 24 h), at most 30; expired at 0 h."""
    if isinstance(now_utc, str):
        now_utc = parse_utc(now_utc)
    left = parse_utc(deleted_at_utc) + timedelta(days=RETENTION_DAYS) - now_utc
    seconds = int(left.total_seconds())
    return min(seconds // 86400, RETENTION_DAYS), seconds <= 0


def display_key(deleted_at_utc, now_utc):
    """The catalog id a row shows (`trash.row.daysLeft` / `.lessThanDay`), or "expired"."""
    n, expired = days_left(deleted_at_utc, now_utc)
    return "expired" if expired else ("trash.row.lessThanDay" if n < 1 else "trash.row.daysLeft")


# ---- identity -------------------------------------------------------------------------------


def published_identity(path, *, need_sha=True):
    """{"session": "<Recording|Received>-<uuid>"} from our x, else {"sha": opusAudioSha256}.

    need_sha=False returns None for a file without our x instead of computing a digest that
    cannot match (a session identity is being looked for)."""
    from .. import portable
    from . import xschema

    try:
        meeting = portable.read_meeting(str(path))
    except ValueError:
        meeting = None
    if meeting is None:
        # A damaged file stays removable (it has no readable digest): its whole-file digest.
        if not need_sha:
            return None
        return {"sha": _file_digest(path)}
    manifest = meeting.manifest or {}
    extension = manifest.get("x")
    ours = extension.get(xschema.DOMAIN) if isinstance(extension, dict) else None
    if isinstance(ours, dict) and xschema.valid(ours):
        return {"session": f"{ours['origin']}-{ours['sessionId']}"}
    if not need_sha:
        return None
    digest = (manifest.get("integrity") or {}).get("opusAudioSha256") if meeting.is_readable else None
    if not isinstance(digest, str):
        try:
            with open(path, "rb") as stream:
                digest = portable.compute_audio_digest(stream)["sha256"]
        except ValueError:
            digest = _file_digest(path)
    return {"sha": digest}


def _file_digest(path):
    import hashlib

    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        while chunk := stream.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


IDENTITY = published_identity  # the conformance suite replaces it with the model's text identity


def identity_key(identity):
    if not isinstance(identity, dict):
        return None
    return identity.get("session") or ("sha:" + identity["sha"] if isinstance(identity.get("sha"), str) else None)


def _identity_or_none(path, *, need_sha=True):
    try:
        return IDENTITY(path, need_sha=need_sha)
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _regular_file(path):
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except FileNotFoundError:
        return False


def _real_dir(path):
    try:
        return stat.S_ISDIR(os.lstat(path).st_mode)
    except FileNotFoundError:
        return False


def _listdir(path):
    try:
        return sorted(os.listdir(path))
    except (FileNotFoundError, NotADirectoryError):
        return []


def root_files(root):
    """Every live root `.opus` name (regular files only; dotfiles are not meetings)."""
    return [n for n in _listdir(root) if not n.startswith(".") and n.endswith(".opus") and _regular_file(root / n)]


def live_representations(root, *, want=None):
    """(identity key, root-relative path) for every live representation: each root `.opus`
    file, each working folder in `.cassini/sessions/`, each v1 root meeting folder. The trash
    is never live. `want` (an identity key) skips reading files that cannot match it."""
    root = Path(root)
    out = []
    for name in _listdir(root):
        if name.startswith("."):
            continue
        path = root / name
        if name.endswith(".opus") and _regular_file(path):
            identity = _identity_or_none(path, need_sha=want is None or want.startswith("sha:"))
            key = identity_key(identity)
            if key is not None:
                out.append((key, name))
        elif SESSION_NAME.match(name) and _real_dir(path):
            out.append((name, name))
    for name in _listdir(root / SESSIONS):
        if SESSION_NAME.match(name) and _real_dir(root / SESSIONS / name):
            out.append((name, f"{SESSIONS}/{name}"))
    return out


# ---- entries --------------------------------------------------------------------------------


def _read_entry(path):
    with open(path, "rb") as stream:
        raw = stream.read(MAX_ENTRY_FILE + 1)
    if len(raw) > MAX_ENTRY_FILE:
        raise ValueError("entry.json is too large")
    value = json.loads(raw)
    if not isinstance(value, dict) or value.get("version") != 1:
        raise ValueError("unsupported entry.json")
    return value


@dataclass(frozen=True)
class TrashEntry:
    entry_id: str
    title: str
    deleted_at_utc: str
    recorded_at_utc: str | None
    duration_ms: int | None
    identity: dict
    received: bool = False

    @property
    def key(self):
        return identity_key(self.identity)


def entries(root):
    """The view's rows, newest deletion first (`.purge-*` and unreadable entries are hidden)."""
    root = Path(root)
    rows = []
    for entry_id in _listdir(root / TRASH):
        if entry_id.startswith(".") or entry_id.endswith(".partial"):
            continue
        try:
            value = _read_entry(root / TRASH / entry_id / ENTRY_FILE)
            display = value.get("display") or {}
            title = display.get("title")
            deleted = value["deletedAtUtc"]
            parse_utc(deleted)
            duration = display.get("durationMs")
            recorded = display.get("recordedAtUtc")
            rows.append(
                TrashEntry(
                    entry_id=entry_id,
                    title=title if isinstance(title, str) else "",
                    deleted_at_utc=deleted,
                    recorded_at_utc=recorded if isinstance(recorded, str) else None,
                    duration_ms=duration if type(duration) is int and duration >= 0 else None,
                    identity=value.get("identity") or {},
                    received=bool(display.get("received")),
                )
            )
        except (OSError, ValueError, KeyError, TypeError):
            continue
    rows.sort(key=lambda r: (r.deleted_at_utc, r.entry_id), reverse=True)
    return rows


# ---- planning (under the lock) --------------------------------------------------------------


def _check_entry_id(entry_id):
    if not isinstance(entry_id, str) or str(UUID(entry_id)) != entry_id:
        raise Refused("invalid entry id")
    return entry_id


def plan_trash(root, request):
    """Move to Trash. request: entryId, deletedAtUtc, title, recordedAtUtc, durationMs,
    published (root name or None), session (root-relative path or None), namePrefix,
    activeLease; optional `received` (display only)."""
    root = Path(root)
    if request.get("activeLease"):
        raise Refused("recording or saving holds the activity lease")
    if not request.get("published") and not request.get("session"):
        raise Refused("nothing to move")
    entry_id = _check_entry_id(request["entryId"])
    entry_dir = f"{TRASH}/{entry_id}"
    published = identity = None
    if request.get("published"):
        name = request["published"]
        if "/" in name or name.startswith(".") or not name.endswith(".opus") or not _regular_file(root / name):
            raise Refused("published file is gone")
        identity = _identity_or_none(root / name)
        if identity is None:
            raise Refused("published file is unreadable")
        published = {"name": name, "namePrefix": request.get("namePrefix"), "title": request["title"]}
    session = None
    if request.get("session"):
        path = request["session"]
        folder = path.split("/")[-1]
        if path not in (folder, f"{SESSIONS}/{folder}") or not SESSION_NAME.match(folder):
            raise Refused("not a meeting folder")
        if not _real_dir(root / path):
            raise Refused("working folder is gone")
        if identity is None:
            identity = {"session": folder}
        elif identity.get("session") not in (None, folder):
            raise Refused("published file and working folder have different identities")
        session = {"path": path, "layout": "v1" if "/" not in path else "v2"}
    key = identity_key(identity)
    others = [
        path
        for found, path in live_representations(root, want=key)
        if found == key and path != request.get("published") and path.endswith(".opus")
    ]
    if others and not published:
        raise Refused("a published file with this identity is still in the Library")
    if others and session:
        session = None  # the working folder stays with the copy that stays live
    display = {
        "title": request["title"],
        "recordedAtUtc": request["recordedAtUtc"],
        "durationMs": request["durationMs"],
    }
    if request.get("received"):
        display["received"] = True
    entry = {
        "version": 1,
        "entryId": entry_id,
        "deletedAtUtc": request["deletedAtUtc"],
        "identity": identity,
        "published": published,
        "session": session,
        "display": display,
    }
    steps = [
        {"do": "mkdir", "path": TRASH},
        {"do": "mkdir", "path": entry_dir},
        {"do": "writeFile", "path": f"{entry_dir}/{ENTRY_FILE}", "content": dumps(entry)},
    ]
    if published:
        steps.append({"do": "move", "from": published["name"], "to": f"{entry_dir}/{PUBLISHED_IN_ENTRY}"})
    if session:
        steps.append({"do": "move", "from": session["path"], "to": f"{entry_dir}/{session['path'].split('/')[-1]}"})
    return {"kind": "trash", "entryId": entry_id, "identity": identity}, steps


def restore_name(published, existing, untitled, skip=()):
    """The original name if it is free under the casefold key, else the §2 suffix rule from the
    stored prefix and title (or the original stem when there is no stored prefix)."""
    existing = list(existing) + list(skip)
    taken = {names.fold(n) for n in existing}
    if names.fold(published["name"]) not in taken:
        return published["name"]
    prefix = published.get("namePrefix")
    if isinstance(prefix, str) and names.PREFIX_PATTERN.match(prefix):
        title = published.get("title") if isinstance(published.get("title"), str) else ""
        name, _ = names.file_name(prefix, title, untitled, existing)
        return name
    stem = published["name"][: -len(names.EXTENSION)]
    for n in range(2, 2 + MAX_SUFFIX_RETRIES):
        suffix = f" ({n})"
        budget = names.MAX_BYTES - len(suffix.encode()) - len(names.EXTENSION)
        cut = names.truncate(stem, budget).rstrip(" .")
        name = cut + suffix + names.EXTENSION
        if names.fold(name) not in taken:
            return name
    raise Refused("no free name")


def _untitled():
    from ..strings import text

    return text("library.untitled")


def plan_restore(root, request, untitled=None):
    root = Path(root)
    entry_id = _check_entry_id(request["entryId"])
    entry_dir = f"{TRASH}/{entry_id}"
    try:
        entry = _read_entry(root / entry_dir / ENTRY_FILE)
    except FileNotFoundError:
        raise Refused("no such entry") from None
    except (OSError, ValueError) as error:
        raise Refused(f"unreadable entry: {error}") from None
    key = identity_key(entry.get("identity"))
    if key is None:
        raise Refused("entry has no identity")
    if key in {found for found, _ in live_representations(root, want=key)}:
        raise Refused("a meeting with this identity is already in the Library")
    steps = []
    if entry.get("session"):
        target = entry["session"]["path"]
        folder = target.split("/")[-1]
        if not SESSION_NAME.match(folder) or target not in (folder, f"{SESSIONS}/{folder}"):
            raise Refused("unreadable entry: session path")
        if os.path.lexists(root / SESSIONS / folder) or os.path.lexists(root / folder):
            raise Refused("a meeting with this identity is already in the Library")
        if entry["session"].get("layout") == "v2":
            steps.append({"do": "mkdir", "path": SESSIONS})
        steps.append({"do": "move", "from": f"{entry_dir}/{folder}", "to": target})
    if entry.get("published"):
        name = restore_name(entry["published"], root_files(root), untitled or _untitled())
        steps.append({"do": "move", "from": f"{entry_dir}/{PUBLISHED_IN_ENTRY}", "to": name, "nameFrom": "restoreName"})
    steps += [{"do": "remove", "path": f"{entry_dir}/{ENTRY_FILE}"}, {"do": "removeTree", "path": entry_dir}]
    return {"kind": "restore", "entryId": entry_id, "identity": entry["identity"]}, steps


def plan_purge(root, request):
    root = Path(root)
    entry_id = _check_entry_id(request["entryId"])
    entry_dir = f"{TRASH}/{entry_id}"
    if not _real_dir(root / entry_dir):
        raise Refused("no such entry")
    purge_dir = f"{TRASH}/{PURGE_PREFIX}{entry_id}"
    try:
        entry = _read_entry(root / entry_dir / ENTRY_FILE)
    except (OSError, ValueError):
        entry = None
    steps = [{"do": "move", "from": entry_dir, "to": purge_dir}]
    if entry and entry.get("published"):
        steps.append({"do": "remove", "path": f"{purge_dir}/{PUBLISHED_IN_ENTRY}"})
    folder = ((entry or {}).get("session") or {}).get("path", "").split("/")[-1]
    if folder and SESSION_NAME.match(folder):
        steps.append({"do": "removeTree", "path": f"{purge_dir}/{folder}"})
    steps += [{"do": "remove", "path": f"{purge_dir}/{ENTRY_FILE}"}, {"do": "removeTree", "path": purge_dir}]
    identity = (entry or {}).get("identity") if identity_key((entry or {}).get("identity")) else None
    return {"kind": "purge", "entryId": entry_id, "identity": identity}, steps


# ---- journal entries ------------------------------------------------------------------------


def _new_journal(head, steps):
    created = datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    return dict(op=str(uuid4()), createdAt=created, **head, steps=steps)


def _confined(path, entry_id, *, purge=False):
    base = f"{TRASH}/{entry_id}"
    purge_base = f"{TRASH}/{PURGE_PREFIX}{entry_id}"
    bases = (base, purge_base) if purge else (base,)
    return any(path == b or path.startswith(b + "/") for b in bases)


def _live_end(path):
    """A move's Library end: a root `.opus` name, a v1 root folder or a v2 working folder."""
    if "/" not in path:
        return not path.startswith(".") and (path.endswith(".opus") or bool(SESSION_NAME.match(path)))
    head, _, folder = path.rpartition("/")
    return head == SESSIONS and bool(SESSION_NAME.match(folder))


def check(layout, entry):
    """Structure and confinement of a trash/restore/purge journal entry; ValueError otherwise.

    Every removal is confined to this entry's own trash directory, and every move has one end
    inside it, so a damaged journal can never remove or move anything else in the Library."""
    kind = entry.get("kind")
    if kind not in KINDS or not isinstance(entry.get("createdAt"), str):
        raise ValueError("invalid journal kind")
    entry_id = entry.get("entryId")
    if not isinstance(entry_id, str) or str(UUID(entry_id)) != entry_id:
        raise ValueError("invalid trash entry id")
    identity = entry.get("identity")
    if identity is not None and identity_key(identity) is None:
        raise ValueError("invalid trash identity")
    steps = entry.get("steps")
    if not isinstance(steps, list) or not steps or len(steps) > 16:
        raise ValueError("invalid trash steps")
    purge = kind == "purge"
    for step in steps:
        if not isinstance(step, dict) or step.get("do") not in STEP_KINDS:
            raise ValueError("invalid trash step")
        do = step["do"]
        paths = [step.get("from"), step.get("to")] if do == "move" else [step.get("path")]
        for path in paths:
            layout.resolve(path)  # confined to the root
        if do == "mkdir":
            if step["path"] not in (TRASH, SESSIONS, f"{TRASH}/{entry_id}"):
                raise ValueError("invalid trash mkdir")
        elif do == "writeFile":
            if step["path"] != f"{TRASH}/{entry_id}/{ENTRY_FILE}" or not isinstance(step.get("content"), str):
                raise ValueError("invalid trash writeFile")
        elif do in ("remove", "removeTree"):
            if not _confined(step["path"], entry_id, purge=purge):
                raise ValueError("invalid trash removal")
        else:
            inside = [_confined(p, entry_id, purge=purge) for p in paths]
            if not any(inside) or not all(inside[i] or _live_end(paths[i]) for i in (0, 1)):
                raise ValueError("invalid trash move")
            if step.get("nameFrom") not in (None, "restoreName"):
                raise ValueError("invalid trash move")


# ---- applying and recovering ----------------------------------------------------------------


def _fsync_parent(path):
    with contextlib.suppress(FileNotFoundError):
        fsutil.fsync_dir(Path(path).parent)


def _rename(source, target):
    """One no-replace rename (file or directory), then fsync both directories.

    A failure after the rename itself raises Moved, so the caller knows something moved."""
    if _real_dir(source):
        result, code = fsutil.native()(os.fsencode(source), os.fsencode(target))
        if result != 0:
            if code == errno.EEXIST:
                raise FileExistsError(code, os.strerror(code), str(target))
            if code not in fsutil.UNSUPPORTED:
                raise OSError(code, os.strerror(code), str(target))
            # No RENAME_NOREPLACE here (old kernel, some FUSE): we hold the Library lock.
            if os.path.lexists(target):
                raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), str(target))
            os.rename(source, target)
    else:
        fsutil.rename_noreplace(source, target)
    try:
        _fsync_parent(source)
        if Path(source).parent != Path(target).parent:
            _fsync_parent(target)
    except OSError as error:
        raise Moved(error.errno or errno.EIO, f"Moved, but not yet durable: {error}") from error


def _remove_tree(path):
    if not os.path.lexists(path):
        return
    if _real_dir(path):
        shutil.rmtree(path)
    else:
        os.unlink(path)
    _fsync_parent(path)


def _rewrite_restore_name(layout, step, journal_entry, untitled):
    """The journaled restore name is taken: take the next free suffix and rewrite the entry."""
    entry = _read_entry(layout.resolve(step["from"].rsplit("/", 1)[0] + "/" + ENTRY_FILE))
    rejected = list(journal_entry.get("rejected", []))
    if step["to"] not in rejected:
        rejected.append(step["to"])  # it exists (or the filesystem said EEXIST for it)
    for _attempt in range(MAX_SUFFIX_RETRIES):
        name = restore_name(entry["published"], root_files(layout.root), untitled or _untitled(), skip=rejected)
        step["to"] = name
        for other in journal_entry["steps"]:
            if other.get("nameFrom") == "restoreName":
                other["to"] = name
        journal_entry["rejected"] = rejected
        _write_journal(layout, journal_entry)
        try:
            _rename(layout.resolve(step["from"]), layout.resolve(name))
            return
        except FileExistsError:
            rejected.append(name)  # the filesystem has the last word (§11 Q5 EEXIST retry)
    raise Failed(errno.EEXIST, "No free name to restore this meeting")


def apply_step(layout, step, journal_entry, *, replay=False, untitled=None):
    """Apply one step. With replay=True every step is idempotent (startup recovery)."""
    do = step["do"]
    if do == "mkdir":
        path = layout.resolve(step["path"])
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            if not _real_dir(path):
                raise NotADirectoryError(errno.ENOTDIR, "Not a directory", str(path)) from None
        else:
            _fsync_parent(path)
    elif do == "writeFile":
        fsutil.write_durable(layout.resolve(step["path"]), step["content"].encode())
    elif do == "move":
        source, target = layout.resolve(step["from"]), layout.resolve(step["to"])
        have_source, have_target = os.path.lexists(source), os.path.lexists(target)
        if not replay:
            try:
                _rename(source, target)
            except FileExistsError:
                if step.get("nameFrom") != "restoreName":
                    raise
                _rewrite_restore_name(layout, step, journal_entry, untitled)
        elif have_source and not have_target:
            try:
                _rename(source, target)
            except FileExistsError:
                if step.get("nameFrom") != "restoreName":
                    raise Conflict(f"{step['from']} and {step['to']} both exist") from None
                _rewrite_restore_name(layout, step, journal_entry, untitled)
        elif have_source and have_target:
            if step.get("nameFrom") != "restoreName":
                raise Conflict(f"{step['from']} and {step['to']} both exist")
            _rewrite_restore_name(layout, step, journal_entry, untitled)
        elif not have_source and not have_target:
            journal_entry.setdefault("_missing", []).append(step["from"])
    elif do == "remove":
        path = layout.resolve(step["path"])
        if os.path.lexists(path):
            if _real_dir(path):
                raise IsADirectoryError(errno.EISDIR, "Is a directory", str(path))
            os.unlink(path)
            _fsync_parent(path)
    elif do == "removeTree":
        _remove_tree(layout.resolve(step["path"]))
    else:
        raise ValueError(do)


def _write_journal(layout, journal_entry):
    stored = {k: v for k, v in journal_entry.items() if not k.startswith("_")}
    journal.write(layout, stored)


def _run(layout, journal_entry, untitled=None):
    """Journal first, then each step, then retire (the caller holds the lock)."""
    _write_journal(layout, journal_entry)
    count = 1
    _checkpoint(count)
    applied_move = False
    for step in journal_entry["steps"]:
        try:
            apply_step(layout, step, journal_entry, untitled=untitled)
        except OSError as error:
            # Once anything of the meeting moved (this step's rename included, even when its
            # fsync then failed), entry.json and the journal entry stay for forward recovery.
            if not (applied_move or isinstance(error, Moved) or _step_moved(layout, step)):
                _roll_back_unmoved(layout, journal_entry)
                raise
            raise Failed(error.errno or errno.EIO, f"Stopped after a partial move: {error}") from error
        if step["do"] == "move":
            applied_move = True
        count += 1
        _checkpoint(count)
    journal.retire(layout, journal_entry["op"])
    _checkpoint(count + 1)


def _step_moved(layout, step):
    """A move step whose source is gone and whose target exists did happen, whatever it raised."""
    if step["do"] != "move":
        return False
    try:
        source, target = layout.resolve(step["from"]), layout.resolve(step["to"])
    except ValueError:
        return False
    return not os.path.lexists(source) and os.path.lexists(target)


def _roll_back_unmoved(layout, journal_entry):
    """Nothing of the meeting moved yet: undo what this op created, then retire it."""
    with contextlib.suppress(OSError):
        if journal_entry["kind"] == "trash":
            entry_dir = layout.resolve(f"{TRASH}/{journal_entry['entryId']}")
            (entry_dir / ENTRY_FILE).unlink(missing_ok=True)
            with contextlib.suppress(OSError):
                entry_dir.rmdir()
        journal.retire(layout, journal_entry["op"])


def _checkpoint(count):
    if CHECKPOINT is not None:
        CHECKPOINT(count)


def resume(layout, journal_entry, untitled=None):
    """Replay one pending entry forward; Conflict stops it and keeps its journal entry."""
    for step in journal_entry["steps"]:
        apply_step(layout, step, journal_entry, replay=True, untitled=untitled)
    journal.retire(layout, journal_entry["op"])


def describe(journal_entry):
    return f"{journal_entry['kind']} {journal_entry['entryId']}"


def sweep(layout, pending_entries=None):
    """After replay: remove `.purge-*` leftovers without a pending entry, and rebuild a missing
    entry.json (dated now) for an entry directory without a pending entry. Returns messages."""
    if pending_entries is None:
        pending_entries = [e for e in journal.pending(layout, cleanup=False).entries if e.get("kind") in KINDS]
    busy = {e["entryId"] for e in pending_entries}
    report = []
    trash = layout.root / TRASH
    for name in _listdir(trash):
        path = trash / name
        if name.startswith(PURGE_PREFIX):
            if name[len(PURGE_PREFIX) :] not in busy:
                _remove_tree(path)
                report.append(f"swept {name}")
            continue
        if name.startswith(".") or name.endswith(".partial") or name in busy or not _real_dir(path):
            continue
        if os.path.lexists(path / ENTRY_FILE):
            continue
        if _rebuild_entry(layout, name):
            report.append(f"rebuilt {name}")
    return report


def _rebuild_entry(layout, entry_id):
    try:
        _check_entry_id(entry_id)
    except (Refused, ValueError):
        return False
    path = layout.root / TRASH / entry_id
    children = _listdir(path)
    folder = next((c for c in children if SESSION_NAME.match(c) and _real_dir(path / c)), None)
    has_file = PUBLISHED_IN_ENTRY in children and _regular_file(path / PUBLISHED_IN_ENTRY)
    if not folder and not has_file:
        if not children:
            _remove_tree(path)
        return False
    identity = _identity_or_none(path / PUBLISHED_IN_ENTRY) if has_file else None
    if identity is None:
        identity = {"session": folder} if folder else None
    if identity is None:
        return False
    title = None
    published = None
    if has_file:
        prefix = None
        with contextlib.suppress(Exception):
            from .. import portable
            from . import xschema

            meeting = portable.read_meeting(str(path / PUBLISHED_IN_ENTRY))
            manifest = meeting.manifest or {}
            title = (manifest.get("meeting") or {}).get("title") or meeting.tags.get("TITLE")
            ours = (manifest.get("x") or {}).get(xschema.DOMAIN) or {}
            prefix = ours.get("namePrefix") if isinstance(ours.get("namePrefix"), str) else None
        title = title if isinstance(title, str) and title.strip() else _untitled()
        name, _ = names.file_name(prefix, title, _untitled()) if prefix else (names.sanitize(title, _untitled()), 0)
        if not name.endswith(names.EXTENSION):
            name += names.EXTENSION
        published = {"name": name, "namePrefix": prefix, "title": title}
    session = None
    if folder:
        v1 = not has_file and any(SESSION_NAME.match(n) and _real_dir(layout.root / n) for n in _listdir(layout.root))
        session = {"path": folder if v1 else f"{SESSIONS}/{folder}", "layout": "v1" if v1 else "v2"}
    entry = {
        "version": 1,
        "entryId": entry_id,
        "deletedAtUtc": _now_text(),
        "identity": identity,
        "published": published,
        "session": session,
        "display": {"title": title or folder or _untitled(), "recordedAtUtc": None, "durationMs": None},
    }
    fsutil.write_durable(path / ENTRY_FILE, dumps(entry).encode())
    return True


def recover(layout, *, identity=None, untitled=None):
    """Replay pending trash/restore/purge entries forward (the caller holds the lock).

    Returns (completed journal entries, kept (entry, reason) pairs, messages). `identity` (a
    key such as "Recording-<uuid>") limits the replay to one meeting and skips the sweep."""
    found = journal.pending(layout)
    completed, kept, messages = [], [], []
    for entry in found.entries:
        if entry.get("kind") not in KINDS:
            continue
        if identity is not None and identity_key(entry.get("identity")) != identity:
            continue
        try:
            resume(layout, entry, untitled)
        except Conflict as error:
            kept.append((entry, str(error)))
            messages.append(f"conflict in {describe(entry)}: {error}")
            continue
        except (OSError, ValueError, KeyError, TypeError, Refused) as error:
            kept.append((entry, str(error)))
            messages.append(f"kept {describe(entry)}: {error}")
            continue
        completed.append(entry)
        messages.append(f"replayed {describe(entry)}")
    if identity is None:
        remaining = [e for e in journal.pending(layout, cleanup=False).entries if e.get("kind") in KINDS]
        messages += sweep(layout, remaining)
    return completed, kept, messages


# ---- public operations (each takes the Library lock) ----------------------------------------


def _blocked(layout, key):
    """Finish this identity's own pending work first; True when an entry for it stays pending."""
    if key is None:
        return False
    for entry in journal.pending(layout).entries:
        kind = entry.get("kind")
        if kind in KINDS:
            if identity_key(entry.get("identity")) == key:
                return True
        elif key == f"{entry['identity']['origin']}-{entry['identity']['sessionId']}":
            return True
    return False


def move_to_trash(
    root,
    *,
    published=None,
    session=None,
    title,
    recorded_at_utc=None,
    duration_ms=None,
    name_prefix=None,
    received=False,
    active_lease=False,
    now=None,
    entry_id=None,
    guard=None,
):
    """Move one meeting into a new trash entry; returns its entry id. Refused changes nothing.

    `guard` returns a context manager entered under the Library lock (lock order Library ->
    meeting_store.LOCK), so in-process metadata writers wait for the move."""
    root = Path(root)
    layout = journal.Layout(root)
    request = {
        "entryId": entry_id or str(uuid4()),
        "deletedAtUtc": _now_text(now),
        "title": title,
        "recordedAtUtc": recorded_at_utc,
        "durationMs": duration_ms,
        "published": published,
        "session": session,
        "namePrefix": name_prefix,
        "activeLease": active_lease,
        "received": received,
    }
    with journal.LibraryLock(layout):
        folder_identity = journal.identity_of(session.split("/")[-1]) if session else None
        if folder_identity is not None:
            journal.replay(layout, identity=folder_identity)  # finish this meeting's own pending work
        with guard() if guard else contextlib.nullcontext():
            head, steps = plan_trash(root, request)
            if _blocked(layout, identity_key(head["identity"])):
                raise Refused("an earlier change to this meeting has not finished")
            _run(layout, _new_journal(head, steps))
    return request["entryId"]


def restore(root, entry_id, untitled=None):
    """Restore one entry; returns the restored root file name (or None for case (c))."""
    root = Path(root)
    layout = journal.Layout(root)
    with journal.LibraryLock(layout):
        head, steps = plan_restore(root, {"entryId": entry_id}, untitled)
        if _blocked(layout, identity_key(head["identity"])):
            raise Refused("an earlier change to this meeting has not finished")
        journal_entry = _new_journal(head, steps)
        _run(layout, journal_entry, untitled)
    return next((s["to"] for s in journal_entry["steps"] if s.get("nameFrom") == "restoreName"), None)


def purge(root, entry_id):
    root = Path(root)
    layout = journal.Layout(root)
    with journal.LibraryLock(layout):
        _purge_locked(layout, entry_id)


def _relevant(journal_entry, entry_id, key):
    """A pending journal entry touching this trash entry or its meeting."""
    if journal_entry.get("kind") in KINDS:
        return journal_entry.get("entryId") == entry_id or (
            key is not None and identity_key(journal_entry.get("identity")) == key
        )
    identity = journal_entry.get("identity") or {}
    return key is not None and key == f"{identity.get('origin')}-{identity.get('sessionId')}"


def _settle(layout, entry_id):
    """Finish forward every pending operation of this entry or its meeting (the caller holds
    the lock). Returns (completed purge journal entry of this entry or None, still blocked)."""
    try:
        key = identity_key(_read_entry(layout.root / TRASH / entry_id / ENTRY_FILE).get("identity"))
    except (OSError, ValueError):
        key = None
    found = [e for e in journal.pending(layout).entries if _relevant(e, entry_id, key)]
    if not found:
        return None, False
    finished = None
    for journal_entry in found:
        if journal_entry.get("kind") in KINDS:
            try:
                resume(layout, journal_entry)
            except (Conflict, OSError, ValueError, KeyError, TypeError, Refused):
                continue
            if journal_entry["kind"] == "purge" and journal_entry.get("entryId") == entry_id:
                finished = journal_entry
        else:
            identity = journal_entry["identity"]
            journal.replay(layout, identity=(identity["origin"], identity["sessionId"]))
    blocked = any(_relevant(e, entry_id, key) for e in journal.pending(layout, cleanup=False).entries)
    return finished, blocked


def _purge_locked(layout, entry_id):
    """Purge one entry, but never while an operation on it or its meeting is unfinished: a
    replay after the purge would otherwise recreate the entry around its surviving parts."""
    _check_entry_id(entry_id)
    finished, blocked = _settle(layout, entry_id)
    if blocked:
        raise Refused("an earlier change to this meeting has not finished")
    if finished is not None and not os.path.lexists(layout.root / TRASH / entry_id):
        return {"kind": "purge", "entryId": entry_id, "identity": finished.get("identity")}
    head, steps = plan_purge(layout.root, {"entryId": entry_id})
    _run(layout, _new_journal(head, steps))
    return head


class Incomplete(Refused):
    """Empty Trash purged what it could; some entries stay (an unfinished earlier change)."""

    def __init__(self, purged, kept):
        super().__init__(f"{len(kept)} entries have an unfinished earlier change")
        self.purged, self.kept = purged, kept


def empty_trash(root):
    """One journaled purge per entry, newest deletion first. Returns the purged identities;
    raises Incomplete (carrying them) when an entry had to stay."""
    root = Path(root)
    layout = journal.Layout(root)
    purged, kept = [], []
    with journal.LibraryLock(layout):
        for row in entries(root):
            try:
                purged.append(_purge_locked(layout, row.entry_id).get("identity"))
            except Refused:
                kept.append(row.entry_id)
    if kept:
        raise Incomplete(purged, kept)
    return purged


def expire(root, now=None):
    """Purge every entry whose 30 days are over, as Delete permanently does, without a dialog."""
    root = Path(root)
    if not _real_dir(root / TRASH):
        return []
    now = now or datetime.now(UTC)
    due = [row for row in entries(root) if days_left(row.deleted_at_utc, now)[1]]
    if not due:
        return []
    layout = journal.Layout(root)
    purged = []
    with journal.LibraryLock(layout):
        for row in due:
            with contextlib.suppress(Refused):
                purged.append(_purge_locked(layout, row.entry_id).get("identity"))
    return purged
