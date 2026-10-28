# SPDX-License-Identifier: GPL-3.0-or-later
"""D24 rejoin on disk: stream/downmix/FIR, silence gaps and sum track spans.

Only the speech child imports this module. Long received audio never becomes a
WAV in /tmp or a concatenated in-memory PCM array.
"""

import hashlib
import math
import os
import tempfile
import time
from contextlib import closing, contextmanager
from pathlib import Path

import numpy as np

from .speech_pipeline import audio_chunks


@contextmanager
def rejoin(tracks, *, scratch=None, heartbeat=lambda: None):
    directory = (
        Path(scratch)
        if scratch
        else Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "cassini/diarization"
    )
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    last_notice = time.monotonic()
    # A file descriptor avoids transporting paths to temporary audio; no
    # transcript/capture file is opened for writing by the worker.
    with tempfile.TemporaryFile(dir=directory) as stream:
        frames = 0
        for track in tracks:
            offset = track.get("offsetMs", track.get("startOffsetMs")) or 0
            if type(offset) not in (int, float) or not math.isfinite(offset) or offset < 0:
                raise ValueError("Invalid diarization track offset")
            cursor = math.floor(offset * 16)
            with closing(audio_chunks(track)) as chunks:
                for block in chunks:
                    if not len(block):
                        continue
                    if len(block) > 16000 or block.ndim != 1 or not np.isfinite(block).all():
                        raise ValueError("Invalid diarization PCM block")
                    stream.seek(cursor * 4)
                    previous = stream.read(len(block) * 4)
                    mixed = np.asarray(block, dtype="<f4").copy()
                    if previous:
                        mixed[: len(previous) // 4] += np.frombuffer(previous, dtype="<f4")
                    stream.seek(cursor * 4)
                    stream.write(mixed.tobytes())
                    cursor += len(block)
                    frames = max(frames, cursor)
                    if time.monotonic() - last_notice >= 1:
                        heartbeat()
                        last_notice = time.monotonic()
        stream.flush()
        stream.seek(0)
        fingerprint = hashlib.file_digest(stream, "sha256").hexdigest()
        stream.seek(0)
        yield stream, frames, fingerprint
