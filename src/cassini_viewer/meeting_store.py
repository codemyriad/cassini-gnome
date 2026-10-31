# SPDX-License-Identifier: GPL-3.0-or-later
"""Atomic app-owned metadata; capture audio and session.json remain read-only."""

import contextlib
import json
import os
import tempfile
import threading
from pathlib import Path

from .library import meeting_id
from .library_files import regular
from .library_schema import valid
from .recording_session import strict_json

LOCK = threading.RLock()


def atomic_json(path, value, *, before_commit=None):
    path = Path(path)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", prefix="." + path.name + "-", dir=path.parent, delete=False
    ) as stream:
        temporary = Path(stream.name)
        try:
            os.chmod(temporary, 0o600)
            json.dump(value, stream, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        if before_commit is not None:
            before_commit()
        temporary.replace(path)
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        temporary.unlink(missing_ok=True)


def read_metadata(directory):
    directory = Path(directory)
    identifier = meeting_id(Path(directory))
    if identifier is None:
        raise ValueError("Meeting is outside the Library")
    try:
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        value = published_metadata(directory)
        if value is not None:
            return value
        raise
    try:
        try:
            with regular(descriptor, "meeting.json") as (stream, _info):
                raw = stream.read(1024 * 1024 + 1)
        except FileNotFoundError:
            raw = None
    finally:
        os.close(descriptor)
    if raw is not None:
        if len(raw) > 1024 * 1024:
            raise ValueError("Meeting metadata exceeds limit")
        value = strict_json(raw)
        if not valid(value) or value["id"] != identifier:
            raise ValueError("Cannot edit invalid or newer meeting metadata")
        if directory.name.startswith("Received-") != (value["received"] is not None):
            raise ValueError("Invalid received metadata")
        return value
    if directory.name.startswith("Received-"):
        raise ValueError("Received meeting is not published")
    return dict(
        version=1,
        id=identifier,
        revision=0,
        title=None,
        titleSource="meeting-window",
        notes=[],
        speakers=[],
        transcript=dict(status="none", reference=None),
        etaMinutes=None,
        error=None,
        quoteOverride=None,
        received=None,
    )


def published_metadata(directory):
    """Read a known file's x when its optional working metadata is absent.

    This is a read, not a repair: no folder or sidecar is created. No-x files
    require the shared adoption API and are deliberately not synthesized here.
    """
    from . import portable
    from .library_publication import root_for
    from .library_v2 import journal, publish, xschema

    root = root_for(directory)
    if root is None:
        return None
    origin, identifier = journal.identity_of(directory)
    name = publish.find_file(journal.Layout(root), origin, identifier)
    if name is None:
        return None
    embedded = portable.read_meeting(str(root / name))
    ours = (embedded.manifest or {}).get("x", {}).get(xschema.DOMAIN)
    if not embedded.is_readable or not isinstance(ours, dict) or not xschema.valid(ours):
        raise ValueError("Published Library metadata is not editable")
    if portable.verify_audio(str(root / name), embedded).state != portable.State.OK:
        raise ValueError("Published Library audio does not verify")
    notes = embedded.manifest["x"].get(xschema.NOTES_DOMAIN)
    return dict(xschema.rebuild(ours, notes, session_exists=False), published=True)


def transcript_snapshot(directory):
    """Read a validated working body for the final journaled file commit."""
    from .library import _sidecar_words
    from .portable import INFLATE_CEILING

    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        try:
            with regular(descriptor, "transcript.json") as (stream, _info):
                raw = stream.read(INFLATE_CEILING + 1)
        except FileNotFoundError:
            return None
    finally:
        os.close(descriptor)
    if len(raw) > INFLATE_CEILING:
        raise ValueError("Transcript snapshot exceeds limit")
    value = strict_json(raw)
    _sidecar_words(value)
    return value


def update_metadata(directory, *, before_commit=None, transient=False, transcript_body=None, **changes):
    from .library_edits import commit_metadata, transaction
    from .library_publication import root_for

    directory = Path(directory)
    with transaction(directory):
        if not directory.exists():
            from .library_location import owned_directory

            with contextlib.suppress(ValueError):
                directory = owned_directory(directory)
        value = read_metadata(directory)
        if value.get("published") is True and root_for(directory) is not None and not transient:
            return commit_metadata(directory, value, changes, before_commit=before_commit, words=transcript_body)
        value.update(changes)
        value["revision"] += 1
        if not valid(value):
            raise ValueError("Invalid app-owned metadata update")
        if before_commit is None:
            atomic_json(directory / "meeting.json", value)
        else:
            atomic_json(directory / "meeting.json", value, before_commit=before_commit)
        return value
