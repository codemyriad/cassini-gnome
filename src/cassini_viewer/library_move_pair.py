# SPDX-License-Identifier: GPL-3.0-or-later
"""GNOME D15 paired moves; phases live in the existing Library routing journal.

Copies run outside Library/metadata locks. Publication holds routing, both
Library locks in path order, then the metadata lock. Only after both destination
objects are durable does routing prefer them and source retirement begin.
"""

import contextlib
import errno
import hashlib
import json
import os
import shutil
import stat
from pathlib import Path
from uuid import UUID, uuid4

from .library_move import MoveBusy, Moved, _check_cancelled, _copy_file, _rename, _snapshot, _sync
from .library_publication import root_for
from .library_v2 import journal, names, publish

CHECKPOINT = None
PHASES = ("copying", "staged", "published", "retired")


def validate_record(record):
    try:
        if str(UUID(record["op"])) != record["op"] or record["phase"] not in PHASES:
            raise ValueError("Invalid Library move phase")
        if Path(record["name"]).name != record["name"] or not record["name"].endswith(".opus"):
            raise ValueError("Unsafe Library move filename")
        if type(record["fileRevision"]) is not int or record["fileRevision"] < 0:
            raise ValueError("Invalid Library move revision")
        for key in ("audioSha256", "sourceTree"):
            value = record[key]
            if key == "sourceTree" and value is None:
                continue
            if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
                raise ValueError("Invalid Library move digest")
        keys = ("sourceFile",) if record["phase"] == "copying" else ("sourceFile", "stagedFile")
        for key in keys:
            value = record[key]
            if not isinstance(value, list) or len(value) != 4 or any(type(n) is not int for n in value):
                raise ValueError("Invalid Library move fingerprint")
        if record["phase"] != "copying" and record["sourceTree"] is not None:
            value = record["stagedSession"]
            if not isinstance(value, list) or len(value) != 2 or any(type(n) is not int for n in value):
                raise ValueError("Invalid Library move session fingerprint")
    except (KeyError, TypeError, AttributeError) as error:
        raise ValueError("Invalid Library move checkpoint") from error


def step(name):
    if CHECKPOINT is not None:
        CHECKPOINT(name)


def fingerprint(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("A Library file must be regular")
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns]


def tree(path):
    snapshot = _snapshot(path)
    if len(snapshot) > 4096:
        raise ValueError("The session contains too many files to move safely")
    raw = json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def directory_identity(path):
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError("A session must be a regular directory")
    return [info.st_dev, info.st_ino]


def _source_unchanged(file, source, record):
    try:
        return fingerprint(file) == record["sourceFile"] and (
            record["sourceTree"] is None or tree(source) == record["sourceTree"]
        )
    except FileNotFoundError:
        return False


def _copy(source, target, *, cancelled):
    """Closed audio uses same-inode links where possible; JSON is copied."""
    _check_cancelled(cancelled)
    source, target = Path(source), Path(target)
    if source.suffix in (".opus", ".wav"):
        before = fingerprint(source)
        try:
            os.link(source, target, follow_symlinks=False)
        except OSError as error:
            if error.errno not in (errno.EXDEV, errno.EPERM, errno.EMLINK, errno.EOPNOTSUPP):
                raise
        else:
            if fingerprint(source) != before or fingerprint(target) != before:
                raise MoveBusy("Audio changed while linking its stage")
            return target
    if shutil.disk_usage(target.parent).free < source.stat().st_size + 65536:
        raise OSError(errno.ENOSPC, "Not enough space for the meeting copy")
    return _copy_file(source, target, cancelled=cancelled)


@contextlib.contextmanager
def locked(source_root, target_root):
    from .library_location import routing
    from .meeting_store import LOCK

    with routing(), contextlib.ExitStack() as stack:
        for root in sorted({source_root, target_root}, key=os.fsencode):
            stack.enter_context(journal.LibraryLock(journal.Layout(root)))
        with LOCK:
            yield


def _replay(layout, identity):
    report = journal.replay(layout, identity=identity)
    if report.blocks(*identity) or any(not isinstance(entry, dict) for entry, _reason in report.kept):
        raise MoveBusy("A pending Library operation must finish before moving")


def _integrity(path):
    from . import portable

    embedded = portable.read_meeting(str(path))
    value = (embedded.manifest or {}).get("integrity", {}).get("opusAudioSha256")
    return value.lower() if isinstance(value, str) else None


def _verified_destination(layout, identity, record):
    """Slow recovery verification is outside the locks, then guarded by a stat CAS."""
    from . import portable

    name = publish.find_file(layout, *identity)
    if name is None:
        return None
    path = layout.root / name
    before = fingerprint(path)
    embedded = portable.read_meeting(str(path))
    result = portable.verify_audio(str(path), embedded)
    if result.state != portable.State.OK or result.sha256 != record["audioSha256"]:
        raise ValueError("The moved destination audio no longer verifies")
    if fingerprint(path) != before:
        raise MoveBusy("Destination changed during verification")
    return name, before


def _refresh_moved_metadata(path, session, base_revision):
    """A metadata edit may follow a crashed file publish before the cache arrives."""
    from . import library_schema, meeting_store, portable
    from .library_v2 import xschema
    from .recording_session import strict_json

    embedded = portable.read_meeting(str(path))
    ours = embedded.manifest["x"][xschema.DOMAIN]
    if ours["revision"] <= base_revision:
        return
    current = strict_json((session / "meeting.json").read_bytes()) if (session / "meeting.json").exists() else {}
    value = xschema.rebuild(ours, embedded.manifest["x"].get(xschema.NOTES_DOMAIN))
    value["revision"] = max(value["revision"], current.get("revision", 0))
    if not library_schema.valid(value):
        raise ValueError("The edited destination metadata cannot be rebuilt")
    meeting_store.atomic_json(session / "meeting.json", value)


def _move_pair(source, target, *, checkpoint, record=None, ready=lambda: True, cancelled=None):
    """Move a known root file and its optional session, recovering each durable phase.

    checkpoint(record) runs while routing is held and must durably append the
    complete phase to the existing D15 journal, without reacquiring routing.
    A failed/interrupted publication keeps its record and every unresolved copy.
    """
    source, target = Path(source), Path(target)
    source_root, target_root = root_for(source), root_for(target)
    identity = journal.identity_of(source)
    if source_root is None or target_root is None or identity != journal.identity_of(target):
        raise ValueError("A paired move needs the same managed meeting identity")
    old, new = journal.Layout(source_root), journal.Layout(target_root)
    if source_root == target_root:
        raise ValueError("The move destination is the current Library")
    record = dict(record) if record else None
    if record is not None:
        validate_record(record)
    # A partial publication may have been edited/renamed after the crash. Verify
    # that version without holding either Library lock; never replace it with
    # an older staged copy.
    verified = None
    if record and record["phase"] != "copying":
        candidate_stage = target_root / (".cassini-moving-" + record["op"]) / "audio.opus"
        if not candidate_stage.exists():
            verified = _verified_destination(new, identity, record)
    if record is None or record["phase"] == "copying":
        with locked(source_root, target_root):
            _check_cancelled(cancelled)
            _replay(old, identity)
            _replay(new, identity)
            if not ready():
                raise MoveBusy()
            name = publish.find_file(old, *identity)
            if name is None:
                raise FileNotFoundError("The source Cassini file is missing")
            if target.exists() or any(names.fold(p.name) == names.fold(name) for p in target_root.iterdir()):
                raise FileExistsError("The move destination is occupied")
            if record:
                previous = target_root / (".cassini-moving-" + record["op"])
                if previous.exists():
                    shutil.rmtree(previous)
            record = dict(
                op=str(uuid4()),
                phase="copying",
                name=name,
                sourceFile=fingerprint(source_root / name),
                sourceTree=tree(source) if source.exists() else None,
                fileRevision=publish.file_identity(source_root / name)[2],
                audioSha256=_integrity(source_root / name),
            )
            checkpoint(record)
        stage = target_root / (".cassini-moving-" + record["op"])
        stage.mkdir(mode=0o700)
        _sync(target_root)
        try:
            _copy(source_root / record["name"], stage / "audio.opus", cancelled=cancelled)
            if record["sourceTree"] is not None:
                from functools import partial

                shutil.copytree(
                    source,
                    stage / "session",
                    symlinks=True,
                    copy_function=partial(_copy, cancelled=cancelled),
                )
                for directory, _folders, _files in os.walk(stage / "session", topdown=False, followlinks=False):
                    _sync(directory)
            _sync(stage)
            # _copy_file preserves timestamps after its data fsync. Make those
            # metadata changes durable too before journaling inode/stat CASes.
            for directory, _folders, files in os.walk(stage, followlinks=False):
                for name in files:
                    path = Path(directory) / name
                    if stat.S_ISREG(path.lstat().st_mode):
                        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                        try:
                            os.fsync(descriptor)
                        finally:
                            os.close(descriptor)
            with locked(source_root, target_root):
                if not ready() or not _source_unchanged(source_root / record["name"], source, record):
                    raise MoveBusy("The source changed during its move")
                record.update(
                    phase="staged",
                    stagedFile=fingerprint(stage / "audio.opus"),
                    stagedSession=directory_identity(stage / "session") if record["sourceTree"] is not None else None,
                )
                checkpoint(record)
            step("staged")
        except BaseException as error:
            if record["phase"] == "copying":
                shutil.rmtree(stage, ignore_errors=True)
                if isinstance(error, OSError) and not _source_unchanged(source_root / record["name"], source, record):
                    raise MoveBusy("The source changed while its stage was being copied") from error
            raise
    stage = target_root / (".cassini-moving-" + record["op"])
    file = source_root / record["name"]
    retired_file = source_root / (".cassini-moved-" + record["op"] + ".opus")
    retired_session = source.with_name(".cassini-moved-" + record["op"])
    with locked(source_root, target_root):
        _replay(old, identity)
        _replay(new, identity)
        if record["phase"] == "staged":
            _check_cancelled(cancelled)
            if not ready() or not _source_unchanged(file, source, record):
                raise MoveBusy("The source changed before publication")
            # Check both paths before publishing either; no unrelated target is
            # overwritten, including a cache that appeared while copying.
            if (
                record["sourceTree"] is not None
                and target.exists()
                and directory_identity(target) != record["stagedSession"]
            ):
                raise FileExistsError("The destination session is occupied")
            destination = target_root / record["name"]
            if (stage / "audio.opus").exists():
                if any(names.fold(p.name) == names.fold(record["name"]) for p in target_root.iterdir()):
                    raise FileExistsError("The destination Cassini filename is occupied")
                _rename(stage / "audio.opus", destination)
                _sync(target_root)
                _sync(stage)
                step("file-published")
            else:
                if verified is None or fingerprint(target_root / verified[0]) != verified[1]:
                    raise MoveBusy("The published destination needs verification")
            if record["sourceTree"] is not None and (stage / "session").exists():
                current_name = publish.find_file(new, *identity)
                _refresh_moved_metadata(target_root / current_name, stage / "session", record["fileRevision"])
                _rename(stage / "session", target)
                _sync(target.parent)
                _sync(stage)
                step("session-published")
            record["phase"] = "published"
            checkpoint(record)
            step("published")
        destination_name = publish.find_file(new, *identity)
        expected = verified[1] if verified is not None else record["stagedFile"]
        if destination_name is None or fingerprint(target_root / destination_name) != expected:
            raise MoveBusy("The destination changed; verify it again before retiring sources")
        if record["phase"] == "published":
            if file.exists():
                if fingerprint(file) != record["sourceFile"]:
                    raise ValueError("The original file changed after destination publication; keeping both")
                _rename(file, retired_file)
                _sync(source_root)
                step("source-file-retired")
            if source.exists():
                if tree(source) != record["sourceTree"]:
                    raise ValueError("The original session changed after publication; keeping both")
                _rename(source, retired_session)
                _sync(source.parent)
                step("source-session-retired")
            record["phase"] = "retired"
            checkpoint(record)
            step("retired")
    # Only private, already-retired backups are removed, outside the commit
    # locks. A crash here resumes cleanup; destination edits remain untouched.
    if retired_file.exists():
        if fingerprint(retired_file) != record["sourceFile"]:
            raise ValueError("A retired source changed; keeping the backup")
        retired_file.unlink()
        _sync(source_root)
    if retired_session.exists():
        if tree(retired_session) != record["sourceTree"]:
            raise ValueError("A retired session changed; keeping the backup")
        shutil.rmtree(retired_session)
        _sync(source.parent)
    if stage.exists():
        stage.rmdir()
        _sync(target_root)
    step("cleaned")
    return Moved(source, target)


def move_pair(source, target, *, checkpoint, record=None, ready=lambda: True, cancelled=None):
    """Keep published phases; discard only a provably unpublished private stage."""
    latest = record

    def remember(value):
        nonlocal latest
        checkpoint(value)
        latest = dict(value)

    try:
        return _move_pair(source, target, checkpoint=remember, record=record, ready=ready, cancelled=cancelled)
    except BaseException:
        if latest:
            with contextlib.suppress(OSError, ValueError, KeyError, TypeError, MoveBusy):
                validate_record(latest)
                if root_for(source) is not None and root_for(target) is not None:
                    source_root, target_root = root_for(source), root_for(target)
                    stage = target_root / (".cassini-moving-" + latest["op"])
                    if latest["phase"] == "copying" or (
                        latest["phase"] == "staged" and (stage / "audio.opus").exists()
                    ):
                        with locked(source_root, target_root):
                            latest = dict(latest, phase="copying")
                            with contextlib.suppress(MoveBusy):
                                checkpoint(latest)
                        if stage.exists():
                            shutil.rmtree(stage)
                            _sync(target_root)
        raise
