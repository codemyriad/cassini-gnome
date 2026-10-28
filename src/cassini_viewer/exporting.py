# SPDX-License-Identifier: GPL-3.0-or-later
"""Background, cancellation-aware exports next to the chosen output, never /tmp PCM."""

import ctypes
import errno
import os
import tempfile
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


def export_meeting(meeting, directory, kind="cassini", *, tz=None, cancel=None):
    directory = Path(directory).expanduser().absolute()
    directory.mkdir(parents=True, exist_ok=True)
    check_cancel(cancel)
    stage = encoded = None
    try:
        fd, name = tempfile.mkstemp(prefix=".cassini-export-", dir=directory)
        stage = Path(name)
        with os.fdopen(fd, "wb") as output:
            if kind == "markdown":
                output.write(markdown(model_projection(meeting, tz=tz)).encode())
            elif kind == "cassini":
                policy = None
                original = None
                if meeting.origin == "own":
                    if meeting.lifecycle != "saved" or any(
                        t.start_offset_ms is None and t.frames for t in meeting.tracks
                    ):
                        raise ValueError("Cannot align recording sources with unknown start offsets")
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
                    encode_pcm(meeting.tracks, encoded, policy, cancel)
                    audio_path = encoded
                else:
                    audio_path = meeting.audio_file
                    if audio_path is None:
                        raise ValueError("Meeting has no audio")
                    original = portable.read_meeting(str(audio_path))
                with regular_input(audio_path) as audio:
                    digest = portable.compute_audio_digest(audio, cancelled=lambda: bool(cancel and cancel.is_set()))
                    if original and original.manifest:
                        integrity = original.manifest["integrity"]
                        if integrity.get("opusAudioSha256") == digest["sha256"] and all(
                            integrity.get(k) == digest[k] for k in ("channels", "sampleCount", "durationMs")
                        ):
                            policy = original.manifest.get("audioEncode")
                    tags, _manifest = descriptors(meeting, digest, policy=policy)
                    rewrite_tags(audio, output, tags, cancel)
            else:
                raise ValueError("Unknown export format")
            output.flush()
            os.fsync(output.fileno())
        check_cancel(cancel)
        if kind == "cassini":
            portable_meeting = portable.read_meeting(str(stage))
            if (
                portable.verify_audio(
                    str(stage), portable_meeting, cancelled=lambda: bool(cancel and cancel.is_set())
                ).state
                != portable.State.OK
            ):
                raise ValueError("Exported audio did not verify")
            portable.load_words(portable_meeting, portable_meeting.default_transcript_id)
        check_cancel(cancel)
        result = _publish(stage, directory, file_stem(meeting.title), "md" if kind == "markdown" else "opus")
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        return result
    finally:
        if stage:
            stage.unlink(missing_ok=True)
        if encoded:
            encoded.unlink(missing_ok=True)
