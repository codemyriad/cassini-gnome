# SPDX-License-Identifier: GPL-3.0-or-later
"""Read-only S-12 projection: published root files and hidden working sessions.

This adapter never repairs the Library. Recovery/publish owners perform journal
work on their workers; the scanner only suppresses pending deletions and chooses
one revision for each identity during a commit.
"""

import hashlib
import os
import stat
from contextlib import suppress
from dataclasses import dataclass, replace

from . import portable
from .library_files import FileCache
from .library_v2 import journal, xschema
from .meeting_state import MeetingError
from .strings import text


@dataclass
class RootAudio:
    embedded: object
    duration: int | None
    trusted: bool
    sha256: str | None
    size: int
    words: tuple = ()
    words_damaged: bool = False
    ours: dict | None = None


def root_audio(stream, info):
    from .library import _opus

    embedded, duration, trusted, _words, size = _opus(stream, info)
    digest = None
    if trusted and embedded.manifest:
        digest = embedded.manifest["integrity"]["opusAudioSha256"]
    else:
        with suppress(ValueError):
            digest = portable.compute_audio_digest(stream)["sha256"]
    trusted = trusted and digest is not None
    words, damaged = (), False
    if trusted and embedded.is_readable:
        try:
            words = tuple(portable.load_words(embedded, embedded.default_transcript_id))
        except ValueError:
            damaged = True
    extension = (embedded.manifest or {}).get("x", {})
    ours = extension.get(xschema.DOMAIN) if isinstance(extension, dict) else None
    ours = ours if trusted and isinstance(ours, dict) and xschema.valid(ours) else None
    return RootAudio(embedded, duration, trusted, digest, size, words, damaged, ours)


class PublishedIndex:
    def __init__(self, scanner):
        self.scanner = scanner
        self.files = FileCache()
        self.hidden = None
        self.projected = {}

    def _sessions(self, root, activity):
        from .library import Diagnostic, LibraryScanner, LibrarySnapshot

        layout = journal.Layout(root)
        try:
            for path in (layout.meta, layout.sessions):
                info = path.lstat()
                if not stat.S_ISDIR(info.st_mode):
                    raise ValueError("unsafe-sessions-directory")
        except FileNotFoundError:
            return LibrarySnapshot(), None, True
        except (OSError, ValueError):
            return LibrarySnapshot(diagnostics=(Diagnostic(layout.meta, "unsafe-sessions-directory"),)), None, False
        if self.hidden is None or self.hidden.root != layout.sessions:
            self.hidden = LibraryScanner(layout.sessions, include_published=False)
        return self.hidden.scan(capture_activity=activity), layout.sessions, True

    def combine(self, base, activity):
        from .library import Diagnostic, LibrarySnapshot, _sort

        root = self.scanner.root.resolve()
        hidden, sessions, safe_sessions = self._sessions(root, activity)
        layout = journal.Layout(root)
        pending = journal.pending(layout)
        deleted = {
            (entry["identity"]["origin"], entry["identity"]["sessionId"])
            for entry in pending.entries
            if entry["kind"] == "delete"
        }
        raw = {meeting.directory.name: meeting for meeting in hidden.meetings}
        rows = {self._identity(meeting): meeting for meeting in (*base.meetings, *hidden.meetings)}
        diagnostics = [*base.diagnostics, *hidden.diagnostics]
        published = {}
        names = set()
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            for name in sorted(os.listdir(fd)):
                if name.startswith(".") or not name.endswith(".opus"):
                    continue
                path = root / name
                try:
                    if not stat.S_ISREG(os.stat(name, dir_fd=fd, follow_symlinks=False).st_mode):
                        continue
                    names.add(name)
                    audio = self.files.get(fd, path, "published-opus", root_audio)
                    own = audio.ours
                    working = raw.get(f"{own['origin']}-{own['sessionId']}") if own else None
                    active = activity.get(own["sessionId"]) if own else None
                    cached = self.projected.get(path)
                    if (
                        cached
                        and cached[0] is audio
                        and cached[1] is working
                        and cached[2:4] == (active, safe_sessions)
                    ):
                        meeting, identity, revision = cached[4]
                    else:
                        projection = self._meeting(path, audio, raw, activity, safe_sessions)
                        self.projected[path] = audio, working, active, safe_sessions, projection
                        meeting, identity, revision = projection
                except FileNotFoundError:
                    continue  # Another publisher finished a rename during this scan.
                except (OSError, ValueError, KeyError, TypeError):
                    diagnostics.append(Diagnostic(path, "published-file-unreadable"))
                    # A damaged regular *.opus remains visible and removable.
                    try:
                        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                    if not stat.S_ISREG(info.st_mode):
                        continue
                    meeting = self._broken(path, info)
                    identity, revision = ("unreadable", meeting.id), -1
                # Replace/rename can expose both names. Highest committed file
                # revision wins; an exact tie is stable by filename.
                previous = published.get(identity)
                if previous is None or revision > previous[0]:
                    published[identity] = (revision, meeting)
        finally:
            os.close(fd)
        self.files.entries = {key: value for key, value in self.files.entries.items() if key[0].name in names}
        self.projected = {path: value for path, value in self.projected.items() if path.name in names}
        for identity, (_revision, meeting) in published.items():
            rows[identity] = meeting
        for identity in deleted:
            rows.pop(identity, None)
        for identity, meeting in list(rows.items()):
            if identity not in published and meeting.metadata.get("published") is True:
                rows[identity] = replace(
                    meeting,
                    interrupted_at_ms=None,
                    published_missing=True,
                    publication_notice="library.publishedMissing",
                    metadata_editable=False,
                    has_audio=False,
                    words=(),
                    preview_turns=(),
                    quote_line=None,
                    diagnostics=(*meeting.diagnostics, Diagnostic(meeting.directory, "published-file-missing")),
                )
        return LibrarySnapshot(
            meetings=tuple(sorted((self._publication_state(m) for m in rows.values()), key=_sort)),
            diagnostics=tuple(diagnostics),
            unavailable=base.unavailable or hidden.unavailable,
            directories=tuple(
                dict.fromkeys((*base.directories, *hidden.directories, *((sessions,) if sessions is not None else ())))
            ),
        )

    @staticmethod
    def _publication_state(meeting):
        from .publication_state import project

        return project(meeting)

    @staticmethod
    def _identity(meeting):
        return ("Recording" if meeting.origin == "own" else "Received", meeting.id)

    @staticmethod
    def _broken(path, info):
        from .library import Meeting

        identifier = hashlib.sha256(f"{info.st_dev}:{info.st_ino}".encode()).hexdigest()
        return Meeting(
            id=identifier,
            directory=path,
            collection_key="unreadable/" + identifier,
            title=path.stem,
            started_at_ms=None,
            duration_ms=None,
            origin="received",
            transcript="damaged",
            error=MeetingError("transcript-damaged", None),
            metadata_editable=False,
            published_file=path,
        )

    def _meeting(self, path, audio, raw, activity, safe_sessions):
        from .library import Meeting, _date, _quote, _speakers
        from .speaker_merges import apply_merges

        embedded = audio.embedded
        manifest = embedded.manifest or {}
        extension = manifest.get("x", {})
        extension = extension if isinstance(extension, dict) else {}
        ours = audio.ours
        known = ours is not None
        metadata = {}
        working = None
        if known:
            origin, identifier = ours["origin"], ours["sessionId"]
            directory = journal.Layout(path.parent).session(origin, identifier)
            working = raw.get(directory.name)
            metadata = xschema.rebuild(ours, extension.get(xschema.NOTES_DOMAIN), session_exists=working is not None)
            revision = ours["revision"]
            key = None
        else:
            origin = "Received"
            identifier = audio.sha256 or hashlib.sha256(str(path).encode()).hexdigest()
            directory = path
            revision = -1
            key = "file/" + identifier
        identity = origin, identifier
        if not known and audio.trusted:
            from .portable_context import embedded_notes

            metadata["notes"] = list(embedded_notes(embedded))
        transcript = metadata.get("transcript", {}).get("status")
        if transcript is None:
            transcript = "done" if audio.words else "none"
        if (
            audio.words_damaged
            or not audio.trusted
            or not embedded.is_readable
            and embedded.state != portable.State.PLAIN_AUDIO
        ):
            transcript = "damaged"
        # S-12 Q9: transient status may run ahead; Q14 keeps the file's default
        # words authoritative. Live working-copy display is pending R54.
        local_revision = working.metadata.get("revision") if working else None
        if working and working.metadata_editable and type(local_revision) is int and local_revision >= revision:
            local = working.metadata
            if local.get("transcript", {}).get("status") in ("queued", "running"):
                metadata = dict(
                    metadata,
                    transcript=local["transcript"],
                    etaMinutes=local.get("etaMinutes"),
                    error=local.get("error"),
                )
                transcript = metadata["transcript"]["status"]
        words = audio.words if transcript not in ("none", "failed", "damaged") else ()
        known_speakers = [speaker["id"] for speaker in metadata.get("speakers", [])]
        known_speakers.extend(speaker.id for speaker in embedded.speakers)
        words = apply_merges(words, metadata.get("speakerMerges", []), known_speakers)
        title = manifest.get("meeting", {}).get("title") or embedded.tags.get("TITLE") or path.stem
        title = title.strip() if isinstance(title, str) else ""
        status_error = metadata.get("error")
        error = MeetingError(**status_error) if status_error else None
        if transcript == "damaged":
            error = MeetingError("transcript-damaged", None)
        preview_key = "v2:" + "/".join(identity)
        preview = self.scanner._preview(preview_key, words)
        lifecycle = activity.get(identifier, "saved") if origin == "Recording" else "saved"
        if lifecycle not in ("recording", "saving"):
            lifecycle = "saved"
        eta = metadata.get("etaMinutes") if transcript in ("queued", "running") else None
        return (
            Meeting(
                id=identifier,
                directory=directory,
                collection_key=key,
                title=title or text("library.untitled"),
                title_is_fallback=not title,
                started_at_ms=working.started_at_ms
                if working
                else _date(embedded.tags.get("DATE")) or _date(embedded.created_at),
                duration_ms=audio.duration,
                lifecycle=lifecycle,
                origin="own" if origin == "Recording" else "received",
                transcript=transcript,
                eta_minutes=eta,
                error=error,
                words=tuple(words),
                speakers=_speakers(words, metadata.get("speakers", []), embedded, stored_labels=True),
                preview_turns=preview,
                turns=self.scanner._preview_cache.get(preview_key, (None, None, ()))[2],
                quote_line=_quote(words, metadata.get("quoteOverride"))
                if transcript in ("done", "queued", "running")
                else None,
                tracks=working.tracks if working else (),
                capture_source=working.capture_source if working else None,
                capture_state=working.capture_state if working else None,
                capture_events=working.capture_events if working else (),
                interrupted_at_ms=working.interrupted_at_ms if working else None,
                audio_file=path,
                published_file=path,
                recording_offset_minutes=working.recording_offset_minutes if working else None,
                audio_size_bytes=audio.size,
                has_audio=audio.duration is not None,
                metadata_editable=(
                    safe_sessions
                    and audio.trusted
                    and not audio.words_damaged
                    and known
                    and (working is None or working.metadata_editable)
                ),
                metadata=metadata,
                notes=tuple(metadata.get("notes", [])),
                extensions=extension,
                diagnostics=working.diagnostics if working else (),
            ),
            identity,
            revision,
        )
