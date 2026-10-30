# SPDX-License-Identifier: GPL-3.0-or-later
"""S-12 §4: build and durably publish the Cassini file for a stopped recording session.

finalize() composes names, the journal, the mixdown and the durable publish. It is
not called by the app yet (step 3a); Stop still works as in the v1 Library.

The session is `<Library>/.cassini/sessions/Recording-<uuid>/` with session.json
(capture-owned, read only) and meeting.json (the working copy; defaults when
absent). The capture writers must have stopped (§4.1); the mixdown runs without the Library lock. The published file holds:

- the mono 48 kHz D8 mixdown (§4.3/§4.4), checked by G12 and the format's digest,
  its playable length equal to the timeline within one Opus frame (G13);
- TITLE, manifest meeting.title = the displayed title; DATE = the start with its
  offset, seconds floored (§11 Q8); meeting.recordedAtLocal from a known start;
- the default `words` transcript with the empty body (§3) and no speakers yet;
- x["codemyriad.io"] (x-schema.json, MAPPING.md) at the new revision, the notes at
  x["gocassini.com"].notes and, for an unknown offset, its recovery marker (D19).

meeting.json then gets published/namePrefix/namePrefixSource at that same revision.
"""

import os
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .. import library_schema, meeting_store, portable
from ..library_files import regular
from ..portable_writer import WORDS_MIME, chunk_set, file_tags, rewrite_tags
from ..recording_details import disk_reserve
from ..recording_session import strict_json
from ..strings import text
from . import edit, journal, mixdown, names, publish, xschema

SESSION_LIMIT = 16 << 20
HEADER_ALLOWANCE = 1 << 20
EMPTY_WORDS = dict(format=portable.WORDS_FORMAT, wordCount=0, items=[])


class LowDisk(OSError):
    """§4.2: not enough free space for the mixdown; nothing was published and every WAV is kept."""

    def __init__(self, needed, free):
        super().__init__(f"Not enough free space: {needed} bytes needed, {free} available")
        self.needed, self.free = needed, free


class DurationMismatch(ValueError):
    """G13: the encoded stream's playable length is not the timeline's within one Opus frame."""


@dataclass
class Result:
    name: str  # file name in the Library root
    revision: int
    already_published: bool = False
    missing_sources: list = field(default_factory=list)  # tracks with zero samples (G27), reported at Stop
    offsets_assumed_zero: bool = False  # D19 recovery assumption, keep the warning visible
    problem: str | None = None  # session.json's capture problem (sticky write errors, G26)


def local_zone():
    """The device's IANA zone name, used only for a legacy start without a recorded offset.

    The app should pass its own zone; this fallback reads TZ, the /etc/localtime link and
    /etc/timezone, in that order, and ends at UTC.
    """
    from zoneinfo import available_timezones

    known = available_timezones()
    candidates = [os.environ.get("TZ", "").lstrip(":")]
    try:
        target = os.readlink("/etc/localtime")
        candidates.append(target.split("zoneinfo/", 1)[1] if "zoneinfo/" in target else "")
    except OSError:
        pass
    try:
        with open("/etc/timezone", encoding="utf-8") as stream:
            candidates.append(stream.read(256).strip())
    except OSError:
        pass
    return next((zone for zone in candidates if zone in known), "UTC")


def space_needed(total_samples, choice, statvfs_result):
    """§4.2: the mixdown estimate (the encoded mix plus its staged copy) plus 10 %, plus the D7 Q5 reserve."""
    seconds = total_samples / mixdown.RATE
    estimate = 2 * (seconds * choice["bitrateBps"] / 8 + HEADER_ALLOWANCE)
    return int(estimate * 1.1) + disk_reserve(statvfs_result.f_blocks * statvfs_result.f_frsize)


def read_session(directory):
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        with regular(descriptor, "session.json") as (stream, _info):
            raw = stream.read(SESSION_LIMIT + 1)
    finally:
        os.close(descriptor)
    if len(raw) > SESSION_LIMIT:
        raise ValueError("session.json exceeds its limit")
    value = strict_json(raw)
    if not isinstance(value, dict):
        raise ValueError("invalid session.json")
    return value


def start_of(session, meeting, zone, now):
    """(namePrefix, namePrefixSource or None, DATE or None, recordedAtLocal or None)."""
    start = session.get("createdAt")
    try:
        instant = names.parse_utc(start) if isinstance(start, str) else None
    except ValueError:
        instant = None
    offset = session.get("startedAtUtcOffsetMinutes")
    if instant is None:
        spec = dict(kind="unknown-start", importUtc=now.strftime("%Y-%m-%dT%H:%M:%SZ"), libraryTimeZone=zone)
    else:
        spec = dict(kind="capture", startUtc=start, utcOffsetMinutes=offset, libraryTimeZone=zone)
    prefix, minutes, source = names.prefix_for(spec)
    if isinstance(meeting.get("namePrefix"), str):  # chosen once and kept (§2)
        prefix, source = meeting["namePrefix"], meeting.get("namePrefixSource")
    if instant is None:
        return prefix, source, None, None
    local = (instant + timedelta(minutes=minutes)).replace(microsecond=0, tzinfo=None)
    sign = "-" if minutes < 0 else "+"
    date = local.strftime("%Y-%m-%dT%H:%M:%S") + f"{sign}{abs(minutes) // 60:02d}:{abs(minutes) % 60:02d}"
    return prefix, source, date, local.strftime("%Y-%m-%dT%H:%M:%S")


def library_manifest(*, digest, title, created, recorded_at_local, choice, words_ref, extension):
    shape = dict(
        sampleRate=48000,
        channels=digest["channels"],
        sampleCount=digest["sampleCount"],
        durationMs=digest["durationMs"],
    )
    meeting = dict(id="mtg_" + digest["sha256"], title=title, createdAtUtc=created, durationMs=shape["durationMs"])
    if recorded_at_local is not None:
        meeting["recordedAtLocal"] = recorded_at_local
    manifest = dict(
        kind="cassini-portable-meeting",
        version=1,
        profile="ogg-opus",
        meeting=meeting,
        audio=dict(container="ogg", codec="opus", **shape),
        integrity=dict(matchPolicy="exact-opus-audio-v1", opusAudioSha256=digest["sha256"], **shape),
        speakers=[],
        transcripts=[dict(id="words", default=True, format=portable.WORDS_FORMAT, wordCount=0, payloadRef=words_ref)],
        audioEncode=choice,
    )
    if extension:
        manifest["x"] = extension
    return manifest


@dataclass(frozen=True)
class Plan:
    """What the file says about one meeting.json revision."""

    base: int
    new: int
    update: dict  # meeting.json members the commit sets
    prefix: str
    title: str
    date: str | None
    recorded_at_local: str | None
    extension: dict
    assumed: bool


def plan(session, meeting, timeline, *, zone, now, untitled):
    prefix, source, date, recorded_at_local = start_of(session, meeting, zone, now)
    update = dict(published=True, namePrefix=prefix)
    if source is not None and source != "persisted":
        update["namePrefixSource"] = source
    updated = dict(meeting, **update, revision=meeting["revision"] + 1)
    updated.pop("published")
    if not library_schema.valid(updated):
        raise ValueError("Invalid meeting metadata")
    automatic = session.get("title") if isinstance(session.get("title"), str) else None
    automatic = automatic[:500] if automatic else None
    ours = xschema.private_data(updated, origin="Recording", automatic_title=automatic, tracks=timeline.tracks)
    if not xschema.valid(ours):
        raise ValueError('x["codemyriad.io"] does not match its schema')
    extension = {xschema.DOMAIN: ours}
    notes = xschema.notes_domain(None, updated.get("notes"))
    assumed = any(span.offset_assumed for span in timeline.spans)
    if assumed:
        notes["recovery"] = dict(offsetsAssumedZero=True)
    if notes:
        extension[xschema.NOTES_DOMAIN] = notes
    return Plan(
        base=meeting["revision"],
        new=meeting["revision"] + 1,
        update=update,
        prefix=prefix,
        title=edit.display_title(updated, automatic, untitled),
        date=date,
        recorded_at_local=recorded_at_local,
        extension=extension,
        assumed=assumed,
    )


def tag(audio_path, stage, digest, plan, choice, now):
    """Write `audio_path`'s Ogg Opus stream with the plan's tags to `stage` (new, fsynced, validated)."""
    words_tags, words_ref = chunk_set("CASSINI_TX_WORDS_PAYLOAD_", EMPTY_WORDS, WORDS_MIME)
    created = now.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    manifest = library_manifest(
        digest=digest,
        title=plan.title,
        created=created,
        recorded_at_local=plan.recorded_at_local,
        choice=choice,
        words_ref=words_ref,
        extension=plan.extension,
    )
    tags = file_tags(manifest, words_tags, date=plan.date or created)
    with open(audio_path, "rb") as audio, open(stage, "xb") as output:
        rewrite_tags(audio, output, tags)
        output.flush()
        os.fsync(output.fileno())
    edit.verify(stage, digest=digest["sha256"])


def build(stage, timeline, choice, plan, now, cancel=None):
    """Mix, encode, tag and validate the complete file at `stage`; returns the audio digest."""
    mix = stage.with_name(stage.name.split(".", 1)[0] + ".mix.partial")
    try:
        expected = mixdown.encode(timeline.spans, mix, choice, cancel)
        publish.step("mixed")
        with open(mix, "rb") as audio:
            digest = portable.compute_audio_digest(audio)
        if abs(digest["sampleCount"] - expected) > mixdown.FRAME:
            raise DurationMismatch(f"Encoded {digest['sampleCount']} samples for a {expected}-sample timeline")
        tag(mix, stage, digest, plan, choice, now)
    finally:
        mix.unlink(missing_ok=True)
    return digest


def _published(layout, session_id, meeting):
    name = publish.find_file(layout, "Recording", session_id)
    if name is None:
        raise FileNotFoundError("The meeting was published but its file is missing")
    return Result(name=name, revision=meeting["revision"], already_published=True)


def _reconcile(layout, session_id):
    report = journal.replay(layout, identity=("Recording", session_id))
    if report.blocks("Recording", session_id):
        raise publish.PendingCommit("An earlier publish of this meeting has not finished")


def finalize(root, session_id, *, untitled=None, zone=None, now=None, statvfs=os.statvfs, cancel=None):
    """Publish the Cassini file of the stopped recording `session_id`; returns a Result.

    Idempotent (§11 step-3a Q6, startup re-finalize): an already published meeting
    returns its file; a pending journal entry is reconciled first; a build left by a
    dead process is cleaned up by recovery, and the next call builds again.

    The mixdown and encode run without the Library lock (§11 step-3a Q10). The lock
    covers reconciliation, then the journal and commit, which re-check the
    preconditions: not published meanwhile, the same meeting.json revision (else
    the stage is re-tagged from the current one, audio unchanged), a free name.

    Raises EmptyRecording (no source has samples), LowDisk, or ValueError for a
    damaged session, all before anything is published; PendingCommit when the file
    was published but the commit did not finish (recovery finishes it).
    """
    layout = journal.Layout(Path(root).absolute())
    origin = "Recording"
    untitled = untitled or text("library.untitled")
    zone = zone or local_zone()
    now = now or datetime.now(UTC)
    directory = layout.session(origin, session_id)
    with journal.LibraryLock(layout):
        _reconcile(layout, session_id)
    with meeting_store.LOCK:
        meeting = meeting_store.read_metadata(directory)
    if meeting.get("published") is True:
        return _published(layout, session_id, meeting)
    session = read_session(directory)
    timeline = mixdown.session_spans(directory, session)
    if not timeline.spans:
        raise mixdown.EmptyRecording("No audio samples were recorded")
    choice = mixdown.encode_choice(timeline.spans)
    usage = statvfs(layout.root)
    needed = space_needed(mixdown.total_samples(timeline.spans), choice, usage)
    free = usage.f_bavail * usage.f_frsize
    if free < needed:
        raise LowDisk(needed, free)
    first = plan(session, meeting, timeline, zone=zone, now=now, untitled=untitled)
    with journal.Build(layout) as job:
        publish.step("building")
        digest = build(job.stage, timeline, choice, first, now, cancel)
        publish.step("stage-written")
        with journal.LibraryLock(layout):
            _reconcile(layout, session_id)
            with meeting_store.LOCK:
                current = meeting_store.read_metadata(directory)
            if current.get("published") is True:
                return _published(layout, session_id, current)
            final = first
            if current["revision"] != meeting["revision"]:
                # meeting.json changed while mixing: same audio, tags from the current revision.
                final = plan(session, current, timeline, zone=zone, now=now, untitled=untitled)
                retagged = job.stage.with_name(job.op + ".retag.partial")
                tag(job.stage, retagged, digest, final, choice, now)
                os.replace(retagged, job.stage)
            name = publish.publish_new(
                layout,
                origin=origin,
                session_id=session_id,
                base_revision=final.base,
                new_revision=final.new,
                name=dict(prefix=final.prefix, title=final.title, untitled=untitled),
                meeting_update=final.update,
                stage=job.stage,
            )
    problem = session.get("problem") if isinstance(session.get("problem"), str) else None
    return Result(
        name=name,
        revision=final.new,
        missing_sources=list(timeline.empty),
        offsets_assumed_zero=final.assumed,
        problem=problem,
    )
