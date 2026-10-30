# SPDX-License-Identifier: GPL-3.0-or-later
"""S-12 §4.6 and §7: durable publish, replace-in-place, rename-with-replace and delete.

Every operation runs under the Library lock and is journaled before any copy is
touched. Commit orders:

publish  journal -> stage (complete, fsynced, validated) -> journal "staged" ->
         no-replace rename into the root (renameat2 RENAME_NOREPLACE; each candidate
         name is journaled before it is tried; EEXIST takes the next suffix, at most
         99 retries, §11 Q5) -> fsync root and staging -> meeting.json -> retire.
replace  journal -> stage -> "staged" -> atomic replacing rename onto the live file
         (it is the target) -> fsync -> meeting.json -> retire.
rename   journal -> stage -> "staged" -> no-replace rename to the new name -> fsync ->
         meeting.json -> remove the old name -> fsync -> retire.
delete   journal intent -> remove the file -> fsync -> remove the session -> fsync -> retire.

A failure before the rename leaves nothing in the root: the stage is discarded
and the journal entry retired, while every source stays where it was. A failure
after it is a PendingCommit: the entry stays and is reconciled (resume) before
any retry. Nothing here deletes a published file to fake a rollback, and a stage
that conflicts with what is live is kept.

CHECKPOINT is a test hook: called with each step name, it lets the crash matrix
SIGKILL a process between any two steps.
"""

import contextlib
import os
import shutil

from .. import library_schema, meeting_store, portable
from . import fsutil, journal, names

MAX_RETRIES = 99
CHECKPOINT = None


class Kept(RuntimeError):
    """A journal entry that cannot finish safely now; it and its stage stay for later."""


class PendingCommit(RuntimeError):
    """The new file is published but the operation has not finished; recovery will finish it."""


class NamesExhausted(FileExistsError):
    """Every candidate name up to the 99th retry already existed."""


def step(name):
    if CHECKPOINT is not None:
        CHECKPOINT(name)


def file_identity(path):
    """(origin, sessionId, revision) from a Library file's x["codemyriad.io"], or None."""
    try:
        meeting = portable.read_meeting(str(path))
    except (OSError, ValueError):
        return None
    x = (meeting.manifest or {}).get("x")
    ours = x.get("codemyriad.io") if isinstance(x, dict) else None
    if not isinstance(ours, dict) or ours.get("schema") != 1:
        return None
    return ours.get("origin"), ours.get("sessionId"), ours.get("revision")


def find_file(layout, origin, session_id):
    """The root file name that carries this identity (highest revision), or None. Reads headers only."""
    best = None
    for name in sorted(os.listdir(layout.root)):
        path = layout.root / name
        if name.startswith(".") or not name.endswith(".opus") or path.is_symlink() or not path.is_file():
            continue
        found = file_identity(path)
        if found and found[:2] == (origin, session_id) and (best is None or found[2] > best[1]):
            best = name, found[2]
    return best[0] if best else None


def _session(layout, entry):
    return layout.resolve(entry["session"]) if entry.get("session") else None


def _meeting(layout, entry):
    """Apply entry["meetingUpdate"] to meeting.json at the new revision, idempotently.

    meeting.json may have run ahead with transient writes (§11 Q9): the update
    then lands on top with a higher revision, never lowering it.
    """
    directory = _session(layout, entry)
    if directory is None:
        return
    update, revision = entry.get("meetingUpdate") or {}, entry["newRevision"]
    with meeting_store.LOCK:
        current = meeting_store.read_metadata(directory)
        if current["revision"] >= revision and all(current.get(k) == v for k, v in update.items()):
            return
        value = dict(current, **update)
        value["revision"] = revision if current["revision"] < revision else current["revision"] + 1
        if not library_schema.valid(value):
            raise ValueError("Invalid meeting metadata update")
        meeting_store.atomic_json(directory / "meeting.json", value)


def _discard(layout, entry):
    """Drop an unpublished operation's own stage and temporaries, then retire it."""
    for name in os.listdir(layout.staging):
        if name.split(".", 1)[0] == entry["op"]:
            (layout.staging / name).unlink(missing_ok=True)
    fsutil.fsync_dir(layout.staging)
    journal.retire(layout, entry["op"])


def _identity(entry):
    return entry["identity"]["origin"], entry["identity"]["sessionId"]


def _matches(path, entry, revision):
    found = file_identity(path)
    return found is not None and found == (*_identity(entry), revision)


def _place(layout, entry):
    """No-replace rename of the stage into the root under a free name (§2, §11 Q5)."""
    stage = layout.resolve(entry["stage"])
    rejected = list(entry.get("rejected", ()))
    spec = entry["name"]
    old = entry.get("oldPath")
    for _attempt in range(MAX_RETRIES + 1):
        existing = os.listdir(layout.root) + rejected
        name, _clean = names.file_name(spec["prefix"], spec["title"], spec["untitled"], existing, old)
        if name == old:  # §11 Q5: a name equal to the meeting's own is never a target for no-replace
            rejected.append(name)
            continue
        entry.update(newPath=name, rejected=rejected)
        journal.write(layout, entry)
        step("before-rename")
        try:
            fsutil.rename_noreplace(stage, layout.root / name)
        except FileExistsError:
            rejected.append(name)
            continue
        return name
    entry["rejected"] = rejected
    raise NamesExhausted(f"No free name after {MAX_RETRIES} retries")


def _settle(layout, entry):
    """Steps after the new file is in place; each is idempotent."""
    fsutil.fsync_dir(layout.root)
    fsutil.fsync_dir(layout.staging)
    step("after-fsync")
    _meeting(layout, entry)
    step("after-meeting")
    if entry["kind"] == "rename":
        old = layout.root / entry["oldPath"]
        if old.exists() or old.is_symlink():
            if not _matches(old, entry, entry["baseRevision"]):
                raise Kept("The old name now holds another file; it was kept")
            old.unlink()
            step("after-remove-old")
        fsutil.fsync_dir(layout.root)
    journal.retire(layout, entry["op"])


def _stage_and_journal(layout, entry, write_stage):
    """Journal the entry, write its stage, then mark it staged. Failures discard the stage."""
    entry.update(phase="prepared", stage=f"{journal.META}/staging/{entry['op']}.opus.stage")
    journal.write(layout, entry)
    step("journaled")
    stage = layout.resolve(entry["stage"])
    try:
        write_stage(stage)
        fsutil.fsync_file(stage)
        step("stage-written")
        fsutil.fsync_dir(layout.staging)
        entry["phase"] = "staged"
        journal.write(layout, entry)
    except BaseException:
        with contextlib.suppress(OSError):
            _discard(layout, entry)
        raise
    step("staged")
    return stage


def _after_place_failure(layout, entry, error):
    """Placing failed: if the new name already holds the stage, it is published (pending)."""
    stage = layout.resolve(entry["stage"])
    target = layout.root / entry["newPath"] if entry.get("newPath") else None
    if target is not None and (not stage.exists() or fsutil.same_file(stage, target)) and target.exists():
        raise PendingCommit(f"Published, but the operation did not finish: {error}") from error
    with contextlib.suppress(OSError):
        _discard(layout, entry)
    raise error


def _commit_new_name(layout, entry):
    try:
        _place(layout, entry)
    except BaseException as error:
        _after_place_failure(layout, entry, error)
    step("renamed")
    try:
        _settle(layout, entry)
    except BaseException as error:
        raise PendingCommit(f"Published, but the operation did not finish: {error}") from error
    return entry["newPath"]


def publish_new(layout, *, origin, session_id, base_revision, new_revision, name, meeting_update, write_stage):
    """§4.6: publish a new Library file. `name` = {prefix, title, untitled}; returns the file name."""
    session = layout.session(origin, session_id)
    entry = journal.new_entry(
        "publish",
        origin=origin,
        session_id=session_id,
        session=layout.relative(session),
        oldPath=None,
        newPath=None,
        baseRevision=base_revision,
        newRevision=new_revision,
        name=dict(name),
        meetingUpdate=dict(meeting_update),
    )
    _stage_and_journal(layout, entry, write_stage)
    return _commit_new_name(layout, entry)


def commit_edit(
    layout, *, origin, session_id, old_name, base_revision, new_revision, name, meeting_update, write_stage
):
    """§6/§7: replace a published file with a rewritten stage, renaming it when `name` gives a new one.

    `name` is None when the displayed title did not change (§11 Q10): the file is
    replaced in place. Otherwise the new name is computed ignoring the meeting's own
    current name; when it equals that name the edit is a replace too.
    """
    session = layout.session(origin, session_id)
    kind = "replace"
    if name is not None:
        existing = os.listdir(layout.root)
        candidate, _ = names.file_name(name["prefix"], name["title"], name["untitled"], existing, old_name)
        kind = "replace" if candidate == old_name else "rename"
    entry = journal.new_entry(
        kind,
        origin=origin,
        session_id=session_id,
        session=layout.relative(session) if session.is_dir() else None,
        oldPath=old_name,
        newPath=old_name if kind == "replace" else None,
        baseRevision=base_revision,
        newRevision=new_revision,
        name=dict(name) if name else None,
        meetingUpdate=dict(meeting_update),
    )
    stage = _stage_and_journal(layout, entry, write_stage)
    if kind == "rename":
        return _commit_new_name(layout, entry)
    try:
        _replace(layout, entry, stage)
    except BaseException as error:
        if stage.exists():
            if isinstance(error, Kept):
                raise
            with contextlib.suppress(OSError):
                _discard(layout, entry)
            raise
        raise PendingCommit(f"Replaced, but the operation did not finish: {error}") from error
    step("replaced")
    try:
        _settle(layout, entry)
    except BaseException as error:
        raise PendingCommit(f"Replaced, but the operation did not finish: {error}") from error
    return old_name


def _replace(layout, entry, stage):
    target = layout.root / entry["oldPath"]
    if not _matches(target, entry, entry["baseRevision"]):
        entry["phase"] = "conflict"
        journal.write(layout, entry)
        raise Kept("The live file changed since this edit was staged; the edit was kept")
    os.replace(stage, target)


def delete(layout, *, origin, session_id, file_name):
    """§7 delete: a durable intent, then the file, then the session folder, then retire."""
    session = layout.session(origin, session_id)
    entry = journal.new_entry(
        "delete",
        origin=origin,
        session_id=session_id,
        phase="intent",
        session=layout.relative(session),
        oldPath=file_name,
        newPath=None,
    )
    if file_name is not None:
        found = file_identity(layout.root / file_name)
        entry["baseRevision"] = found[2] if found and found[:2] == (origin, session_id) else None
    journal.write(layout, entry)
    step("journaled")
    _resume_delete(layout, entry)


def _resume_delete(layout, entry):
    if entry.get("oldPath"):
        path = layout.root / entry["oldPath"]
        if path.exists() or path.is_symlink():
            found = file_identity(path)
            if found is None or found[:2] != _identity(entry):
                raise Kept("Another file now has this name; it was kept")
            path.unlink()
        fsutil.fsync_dir(layout.root)
        step("file-removed")
    session = _session(layout, entry)
    if session is not None and journal.identity_of(session) == _identity(entry):
        if session.is_symlink():
            session.unlink()
        elif session.exists():
            shutil.rmtree(session)
        fsutil.fsync_dir(layout.sessions)
        step("session-removed")
    journal.retire(layout, entry["op"])


def resume(layout, entry):
    """Finish or safely discard one journal entry (startup recovery, or before a retry)."""
    kind, phase = entry["kind"], entry.get("phase")
    if kind == "delete":
        return _resume_delete(layout, entry)
    if phase == "conflict":
        raise Kept("A staged edit conflicts with the live file; it was kept")
    stage = layout.resolve(entry["stage"])
    if phase == "prepared":
        # The stage was never confirmed complete and nothing was renamed: discard it.
        return _discard(layout, entry)
    if phase != "staged":
        raise Kept("Unknown journal phase")
    if kind == "replace":
        if stage.exists():
            _replace(layout, entry, stage)
        elif not _matches(layout.root / entry["oldPath"], entry, entry["newRevision"]):
            raise Kept("The replaced file is missing")
        return _settle(layout, entry)
    target = layout.root / entry["newPath"] if entry.get("newPath") else None
    if stage.exists():
        if target is not None and fsutil.same_file(stage, target):
            stage.unlink()  # the link() fallback had linked it already
        else:
            _place(layout, entry)
    elif target is None or not target.exists():
        raise Kept("The published file is missing")
    _settle(layout, entry)
