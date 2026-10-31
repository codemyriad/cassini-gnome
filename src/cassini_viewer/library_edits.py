# SPDX-License-Identifier: GPL-3.0-or-later
"""App metadata transactions using the shared S-12 journal and tag writer.

Lock order is Library -> meeting_store.LOCK. Nested writers on the same worker
reuse the transaction, rather than flocking a second descriptor and deadlocking.
Legacy folders keep their existing metadata-only transaction.
"""

import threading
from contextlib import contextmanager, suppress
from copy import deepcopy
from pathlib import Path

from .library_publication import root_for
from .library_v2 import edit, journal, publish, xschema
from .portable_writer import MANIFEST_MIME, WORDS_MIME, chunk_set

_LOCAL = threading.local()


@contextmanager
def transaction(directory):
    from .meeting_store import LOCK

    directory = Path(directory)
    if not directory.exists():
        from .library_location import owned_directory

        with suppress(ValueError):
            directory = owned_directory(directory)
    root = root_for(directory)
    active = getattr(_LOCAL, "root", None)
    if root is None or root == active:
        with LOCK:
            yield
        return
    if active is not None:
        raise RuntimeError("A metadata transaction cannot edit two Libraries")
    layout = journal.Layout(root)
    identity = journal.identity_of(directory)
    with journal.LibraryLock(layout):
        report = journal.replay(layout, identity=identity)
        if report.blocks(*identity):
            raise publish.PendingCommit("An earlier change to this meeting has not finished")
        _LOCAL.root = root
        try:
            with LOCK:
                yield
        finally:
            _LOCAL.root = None


def commit_metadata(directory, current, changes, *, before_commit=None, words=None):
    """Commit one snapshot; caller holds transaction(directory).

    `words` is supplied only on completion/failure, not on every live snapshot.
    It contains original speaker IDs, preserving Undo. The current default body,
    other transcripts, arbitrary tags, foreign x and audio packets otherwise stay
    byte-identical. No app-side journal or commit ordering is implemented here.
    """
    from . import library, library_schema, portable
    from .strings import text

    root = root_for(directory)
    if root is None or _LOCAL.root != root:
        raise RuntimeError("Published edits require a Library transaction")
    if set(changes) & {"id", "revision", "namePrefix", "namePrefixSource", "published"}:
        raise ValueError("Identity and revision members are not edits")
    layout = journal.Layout(root)
    origin, identifier = journal.identity_of(directory)
    name = publish.find_file(layout, origin, identifier)
    if name is None:
        raise FileNotFoundError("The meeting's Cassini file is missing")
    path = root / name
    live = portable.read_meeting(str(path))
    manifest = deepcopy(live.manifest)
    if not live.is_readable or not manifest:
        raise ValueError("The meeting's Cassini file is damaged")
    ours = manifest.get("x", {}).get(xschema.DOMAIN)
    if not isinstance(ours, dict) or not xschema.valid(ours):
        raise ValueError("The meeting's private metadata is not editable")
    if ours["revision"] > current["revision"]:
        raise edit.FileAhead("The file is ahead of its session metadata")
    updated = dict(current, **changes, revision=max(current["revision"], ours["revision"]) + 1)
    if not library_schema.valid(updated):
        raise ValueError("Invalid app-owned metadata update")
    automatic = ours.get("automaticTitle")
    previous_title = manifest["meeting"]["title"]
    displayed = (
        edit.display_title(updated, automatic, text("library.untitled"))
        if {"title", "titleSource"} & set(changes)
        else previous_title
    )
    extension = manifest["x"]
    extension[xschema.DOMAIN] = xschema.private_data(updated, origin=origin, automatic_title=automatic, base=ours)
    if not xschema.valid(extension[xschema.DOMAIN]):
        raise ValueError("Invalid private Library metadata")
    notes = xschema.notes_domain(extension.get(xschema.NOTES_DOMAIN), updated.get("notes"))
    if notes:
        extension[xschema.NOTES_DOMAIN] = notes
    else:
        extension.pop(xschema.NOTES_DOMAIN, None)
    manifest["meeting"]["title"] = displayed
    replacements, prefixes = {}, [edit.MANIFEST_PREFIX]
    # Speaker names are local overrides; use the same display rule as the
    # Library. Merge history stays in x, original body IDs remain available.
    original_words = portable.load_words(live, live.default_transcript_id)
    if words is not None:
        if not isinstance(words, dict) or words.get("format") != portable.WORDS_FORMAT:
            raise ValueError("Invalid transcript snapshot")
        body = deepcopy(words)
        body["wordCount"] = len(body["items"])
        entry = next(item for item in manifest["transcripts"] if item["id"] == live.default_transcript_id)
        prefix = entry["payloadRef"]["prefix"]
        tags, reference = chunk_set(prefix, body, WORDS_MIME)
        replacements.update(tags)
        prefixes.append(prefix)
        entry.update(wordCount=body["wordCount"], payloadRef=reference)
        original_words = tuple(
            portable.Word(item["speaker"], item["startMs"], item["endMs"], item["text"]) for item in body["items"]
        )
    speakers = library._speakers(original_words, updated["speakers"], live)
    # Retain unknown fields on existing entries, including vendor provenance.
    entries = {item["id"]: item for item in manifest["speakers"]}
    for speaker in speakers:
        entries.setdefault(speaker.id, dict(id=speaker.id))["label"] = speaker.name
    manifest["speakers"] = list(entries.values())
    replacements["CASSINI_SPEAKER_COUNT"] = str(len(entries))
    if displayed != previous_title:
        replacements["TITLE"] = displayed
    tags, _ = chunk_set(edit.MANIFEST_PREFIX, manifest, MANIFEST_MIME)
    replacements.update(tags)
    with path.open("rb") as stream:
        vendor, fields = edit.comment_fields(stream)
        digest = portable.compute_audio_digest(stream)["sha256"]
    fields = edit.edited_fields(fields, replacements, chunk_prefixes=tuple(prefixes))

    def write_stage(stage):
        edit.rewrite(path, stage, fields, vendor)
        edit.verify(stage, digest=digest)
        if before_commit is not None:
            before_commit()  # The shared journal is still pre-publication here.

    if before_commit is not None:
        before_commit()
    publish.commit_edit(
        layout,
        origin=origin,
        session_id=identifier,
        old_name=name,
        base_revision=current["revision"],
        file_revision=ours["revision"],
        new_revision=updated["revision"],
        name=(
            dict(prefix=ours["namePrefix"], title=displayed, untitled=text("library.untitled"))
            if displayed != previous_title
            else None
        ),
        meeting_update={key: updated[key] for key in changes},
        write_stage=write_stage,
    )
    return updated
