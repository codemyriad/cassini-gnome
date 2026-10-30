# SPDX-License-Identifier: GPL-3.0-or-later
"""S-12 §6: edits are tag rewrites of the published file, committed through §7.

The existing comment vector is edited, never rebuilt from the Library's view: every
field keeps its bytes and order except the replaced ones, which are matched
case-insensitively and appended once (no CASSINI_* name is duplicated); replacing
a chunk set removes all of its old descriptor and numbered chunk tags. The vendor
string is kept. portable_writer.rewrite_tags() then rebuilds only the header pages:
OpusHead and every audio packet stay byte-identical, as do pre-skip, granules, EOS
and the playable sample count (G11, G17). Each stage is validated before it is
published (ogg.validate plus the format's digest and manifest checks).
"""

import os
import re
import struct
from copy import deepcopy
from pathlib import Path

from .. import library_schema, meeting_store, portable
from ..portable_writer import MANIFEST_MIME, chunk_set, rewrite_tags
from ..strings import text
from . import journal, ogg, publish, xschema

CHUNK_TAGS = re.compile(r"(?:[0-9]+|MIME|ENCODING|CHUNK_COUNT|SHA256|RAW_BYTES|GZIP_BYTES)\Z")
MANIFEST_PREFIX = "CASSINI_PAYLOAD_"


class FileAhead(ValueError):
    """The file's revision is above meeting.json's with no journal entry (§11 Q9 forbids it)."""


def header_packets(stream):
    """(OpusHead, OpusTags) packets, raw, within the format's 120 MB header bound."""
    stream.seek(0)
    packets, partial, total = [], bytearray(), 0
    while len(packets) < 2:
        header = stream.read(27)
        if len(header) != 27 or header[:4] != b"OggS":
            raise portable.NotOpus("not an Ogg stream")
        lacing = stream.read(header[26])
        payload = stream.read(sum(lacing))
        if len(lacing) != header[26] or len(payload) != sum(lacing):
            raise portable.Truncated("file ends inside the Opus header")
        total += 27 + len(lacing) + len(payload)
        if total > portable.MAX_HEADER:
            raise portable.CassiniError("the comment header is larger than 120 MB")
        offset = 0
        for size in lacing:
            partial += payload[offset : offset + size]
            offset += size
            if size < 255:
                packets.append(bytes(partial))
                partial.clear()
                if len(packets) == 2:
                    break
    return packets[0], packets[1]


def comment_fields(stream):
    """(vendor bytes, [raw field bytes]) of the OpusTags packet, exactly as stored."""
    _head, packet = header_packets(stream)
    if packet[:8] != b"OpusTags":
        raise portable.NotOpus("second packet is not OpusTags")
    try:
        (size,) = struct.unpack_from("<I", packet, 8)
        vendor = packet[12 : 12 + size]
        offset = 12 + size
        (count,) = struct.unpack_from("<I", packet, offset)
        offset += 4
        if count > portable.MAX_COMMENTS:
            raise portable.CassiniError("too many comments")
        fields = []
        for _ in range(count):
            (size,) = struct.unpack_from("<I", packet, offset)
            field = packet[offset + 4 : offset + 4 + size]
            if len(field) != size:
                raise struct.error("comment runs past the end of the header")
            fields.append(field)
            offset += 4 + size
    except struct.error as error:
        raise portable.NotOpus(f"malformed OpusTags header: {error}") from None
    return vendor, fields


def _name(field):
    return field.split(b"=", 1)[0].decode("ascii", "replace").upper()


def edited_fields(fields, replacements, chunk_prefixes=(MANIFEST_PREFIX,)):
    """Drop the replaced names (case-insensitively) and whole replaced chunk sets; append the new ones."""
    replaced = {name.upper() for name in replacements}

    def dropped(name):
        return name in replaced or any(
            name.startswith(prefix) and CHUNK_TAGS.match(name[len(prefix) :]) for prefix in chunk_prefixes
        )

    kept = [field for field in fields if not dropped(_name(field))]
    return kept + [f"{name}={value}".encode() for name, value in replacements.items()]


def verify(path, *, digest=None):
    """Validate a staged Library file: G12 structure, digest, manifest, words and our x. Returns the digest."""
    ogg.validate_path(path)
    meeting = portable.read_meeting(str(path))
    if not meeting.is_readable:
        raise ValueError(f"Staged file is not a readable Cassini file: {meeting.reason}")
    check = portable.verify_audio(str(path), meeting)
    if check.state != portable.State.OK:
        raise ValueError(f"Staged audio does not verify: {check.reason}")
    if digest is not None and check.sha256 != digest:
        raise ValueError("Staged audio differs from the published audio")
    portable.load_words(meeting, meeting.default_transcript_id)
    x = meeting.manifest.get("x", {}).get(xschema.DOMAIN)
    if x is not None and not xschema.valid(x):
        raise ValueError('Staged x["codemyriad.io"] does not match its schema')
    return check.sha256


def display_title(meeting, automatic_title, untitled):
    """The title the Library shows (MAPPING.md): meeting.title, automaticTitle, else the untitled string."""
    for value in (meeting.get("title"), automatic_title):
        if isinstance(value, str) and value.strip():
            return value
    return untitled


def rewrite(source, stage, fields, vendor):
    """Write `source` with a new comment vector to `stage` (new file, fsynced)."""
    with open(source, "rb") as audio, open(stage, "xb") as output:
        rewrite_tags(audio, output, fields, vendor=vendor)
        output.flush()
        os.fsync(output.fileno())


def edit_meeting(root, origin, session_id, changes, *, untitled=None):
    """Apply meeting.json member `changes` (title, titleSource, notes, ...) to the file and meeting.json.

    One journaled operation: a §6 tag rewrite, replaced in place, or renamed when
    the displayed title changed (§11 Q10). Returns the file name.
    """
    if set(changes) & {"id", "revision", "namePrefix", "namePrefixSource", "published"}:
        raise ValueError("Identity and revision members are not edits")
    untitled = untitled or text("library.untitled")
    layout = journal.Layout(Path(root).absolute())
    with journal.LibraryLock(layout):
        report = journal.replay(layout, identity=(origin, session_id))
        if report.blocks(origin, session_id):
            raise publish.PendingCommit("An earlier change to this meeting has not finished")
        session = layout.session(origin, session_id)
        with meeting_store.LOCK:
            meeting = meeting_store.read_metadata(session)
        name = publish.find_file(layout, origin, session_id)
        if name is None:
            raise FileNotFoundError("The meeting's file is missing")
        path = layout.root / name
        live = portable.read_meeting(str(path))
        manifest = deepcopy(live.manifest)
        x_old = manifest["x"][xschema.DOMAIN]
        if x_old["revision"] > meeting["revision"]:
            raise FileAhead("The file is ahead of its session metadata")
        base, new = meeting["revision"], meeting["revision"] + 1
        updated = dict(meeting, **changes)
        updated["revision"] = new
        if not library_schema.valid(updated):
            raise ValueError("Invalid meeting metadata update")
        automatic = x_old.get("automaticTitle")
        displayed = display_title(updated, automatic, untitled)
        old_display = manifest["meeting"]["title"]
        extension = manifest["x"]
        extension[xschema.DOMAIN] = xschema.private_data(updated, origin=origin, automatic_title=automatic, base=x_old)
        notes = xschema.notes_domain(extension.get(xschema.NOTES_DOMAIN), updated.get("notes"))
        if notes:
            extension[xschema.NOTES_DOMAIN] = notes
        else:
            extension.pop(xschema.NOTES_DOMAIN, None)
        manifest["meeting"]["title"] = displayed
        if not xschema.valid(extension[xschema.DOMAIN]):
            raise ValueError('x["codemyriad.io"] does not match its schema')
        replacements, _ = chunk_set(MANIFEST_PREFIX, manifest, MANIFEST_MIME)
        if displayed != old_display:
            replacements = {"TITLE": displayed, **replacements}
        with open(path, "rb") as stream:
            vendor, fields = comment_fields(stream)
            digest = portable.compute_audio_digest(stream)["sha256"]
        fields = edited_fields(fields, replacements)

        def write_stage(stage):
            rewrite(path, stage, fields, vendor)
            verify(stage, digest=digest)

        spec = (
            dict(prefix=x_old["namePrefix"], title=displayed, untitled=untitled) if displayed != old_display else None
        )
        return publish.commit_edit(
            layout,
            origin=origin,
            session_id=session_id,
            old_name=name,
            base_revision=base,
            new_revision=new,
            name=spec,
            meeting_update={k: updated[k] for k in changes},
            write_stage=write_stage,
        )
