# SPDX-License-Identifier: GPL-3.0-or-later
"""Background, cancellation-aware exports next to the chosen output, never /tmp PCM."""

import ctypes
import errno
import os
import tempfile
from dataclasses import replace
from pathlib import Path

from . import portable
from .export_audio import check_cancel, encode_pcm
from .export_model import encode_choice, file_stem, markdown, model_projection
from .portable_writer import descriptors, rewrite_tags
from .received import regular_input


def _publish(stage, directory, stem, suffix):
    libc = ctypes.CDLL(None, use_errno=True)
    rename = getattr(libc, "renameat2", None)
    if rename:
        rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
        rename.restype = ctypes.c_int
    number = 1
    while True:
        candidate = directory / f"{stem}{' ' + str(number) if number > 1 else ''}.{suffix}"
        if rename:
            if rename(-100, os.fsencode(stage), -100, os.fsencode(candidate), 1) == 0:
                return candidate
            code = ctypes.get_errno()
            if code == errno.EEXIST:
                number += 1
                continue
            if code not in (errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP):
                raise OSError(code, os.strerror(code), candidate)
        try:
            os.link(stage, candidate)
        except FileExistsError:
            number += 1
            continue
        stage.unlink()
        return candidate


def assumed_zero(tracks):
    """D19's export-only copies: an unknown track or span offset is mixed at zero."""

    def placed(track):
        spans = getattr(track, "spans", ())
        if track.start_offset_ms is not None and all(span.start_offset_ms is not None for span in spans):
            return track
        offset = 0 if track.start_offset_ms is None else track.start_offset_ms
        return replace(track, start_offset_ms=offset, spans=tuple(placed(span) for span in spans))

    return tuple(placed(track) for track in tracks)


def _require_export_directory(directory):
    directory = Path(directory).expanduser().absolute()
    from .companion import in_flatpak
    from .library_location import _host_storage

    if in_flatpak() and not _host_storage(directory):
        # A path can be writable yet live only in Flatpak's per-launch tmpfs.
        # Only a portal/host-backed destination is an export destination.
        raise PermissionError(errno.EACCES, os.strerror(errno.EACCES), directory)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _fsync_directory(directory):
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_cassini_opus(meeting, output, directory, *, tz=None, cancel=None):
    policy = None
    original = None
    if meeting.lifecycle != "saved":
        raise ValueError("Only saved recordings can be exported")
    encoded = None
    try:
        if meeting.origin == "own" and meeting.audio_file is None:
            # D19's export-only recovery policy does not alter the
            # Library model, capture metadata or playback alignment.
            tracks = assumed_zero(meeting.tracks)
            policy = encode_choice(
                [
                    dict(
                        codec="pcm",
                        sampleRate=t.sample_rate,
                        speechBitrateBps=None,
                        empty=t.frames < t.sample_rate,
                    )
                    for t in meeting.tracks
                ]
            )
            audio_fd, audio_name = tempfile.mkstemp(prefix=".cassini-audio-", suffix=".opus", dir=directory)
            os.close(audio_fd)
            encoded = Path(audio_name)
            encode_pcm(tracks, encoded, policy, cancel)
            audio_path = encoded
        else:
            audio_path = meeting.audio_file
            if audio_path is None:
                raise ValueError("Meeting has no audio")
        with regular_input(audio_path) as audio:
            trusted_source = False
            if meeting.audio_file is not None:
                original = portable.interpret(*portable.read_header(audio))
                trusted_source = original.state == portable.State.PLAIN_AUDIO
                audio.seek(0)
            digest = portable.compute_audio_digest(audio, cancelled=lambda: bool(cancel and cancel.is_set()))
            if original and original.manifest:
                integrity = original.manifest["integrity"]
                if integrity.get("opusAudioSha256") == digest["sha256"] and all(
                    integrity.get(k) == digest[k] for k in ("channels", "sampleCount", "durationMs")
                ):
                    policy = original.manifest.get("audioEncode")
                    trusted_source = True
            tags, _manifest = descriptors(
                meeting,
                digest,
                policy=policy,
                tz=tz,
                allow_recording_start=trusted_source or meeting.audio_file is None,
                recorded_at_local=original.recorded_at_local if original and trusted_source else None,
                recorded_date=original.meeting_start["value"]
                if original
                and trusted_source
                and original.meeting_start
                and original.meeting_start["source"] != "recordedAtLocal"
                else None,
            )
            rewrite_tags(audio, output, tags, cancel)
    finally:
        if encoded:
            encoded.unlink(missing_ok=True)


def verify_cassini_opus(path, *, cancel=None):
    portable_meeting = portable.read_meeting(str(path))
    if (
        portable.verify_audio(str(path), portable_meeting, cancelled=lambda: bool(cancel and cancel.is_set())).state
        != portable.State.OK
    ):
        raise ValueError("Exported audio did not verify")
    portable.load_words(portable_meeting, portable_meeting.default_transcript_id)


def export_meeting(meeting, directory, kind="cassini", *, tz=None, cancel=None):
    directory = _require_export_directory(directory)
    check_cancel(cancel)
    stage = None
    try:
        fd, name = tempfile.mkstemp(prefix=".cassini-export-", dir=directory)
        stage = Path(name)
        with os.fdopen(fd, "wb") as output:
            if kind == "markdown":
                output.write(markdown(model_projection(meeting, tz=tz)).encode())
            elif kind == "cassini":
                write_cassini_opus(meeting, output, directory, tz=tz, cancel=cancel)
            else:
                raise ValueError("Unknown export format")
            output.flush()
            os.fsync(output.fileno())
        check_cancel(cancel)
        if kind == "cassini":
            verify_cassini_opus(stage, cancel=cancel)
        check_cancel(cancel)
        result = _publish(stage, directory, file_stem(meeting.title), "md" if kind == "markdown" else "opus")
        _fsync_directory(directory)
        return result
    except InterruptedError:
        # The portable reader uses InterruptedError for its cancellation hook;
        # the export worker/dialog use concurrent.futures.CancelledError.
        check_cancel(cancel)
        raise
    finally:
        if stage:
            stage.unlink(missing_ok=True)
