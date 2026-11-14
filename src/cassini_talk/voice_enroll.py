# SPDX-License-Identifier: GPL-3.0-or-later
"""D-977 guided test-phrase enrollment: speech measure, private sample file and microphone capture.

Pure helpers (`speech_segments`, `enrol_windows`) never import NumPy or the
engine, so the GTK process can judge "at least 4 s of clear speech" itself.
The sample lives only in a private 0600 cache file while the CAM++ child
reads it, and is deleted straight after: no audio is ever stored for a voice
(D25 §6), nothing enters the Library, and nothing is exported or uploaded.
"""

import contextlib
import logging
import math
import os
import sys
from array import array
from pathlib import Path
from uuid import UUID, uuid4

from .voices_logic import EDGE_TRIM_MS, MAX_WINDOW_MS, MIN_SEGMENT_MS

LOG = logging.getLogger(__name__)
# sound_check's speech rule (the test recording's meter); that module needs GI,
# so the values are repeated here and tested equal.
ACTIVE_DBFS, FRAME_MS, MIN_RUN_FRAMES = -50.0, 32, 5
RATE = 16000
FRAME = RATE * FRAME_MS // 1000  # 512 samples, as the test recording's meter.
MIN_ENROL_MS = 4000  # D25 MIN_REMEMBER_MS: the same bar as Remember this voice.
MAX_ENROL_MS = 30000  # The phrase takes ~8 s; the take stops itself after 30 s.
# A short breath between words is still one run of speech.
MAX_GAP_FRAMES = 12  # 384 ms


def voice_setup_step(tell, voices):
    """The next action after `Remember voices`, from both model snapshots.

    `download-tell` / `download-voices` start the one model that is missing,
    `wait` keeps the request open (a download or check is running, or a
    failed one waits for Try again), and `done` ends it.
    """
    if not tell.get("downloaded"):
        return "wait" if tell.get("downloading") or tell.get("busy") or tell.get("error") else "download-tell"
    if not tell.get("ready"):
        return "wait"
    if voices.get("downloaded") or voices.get("downloading"):
        return "done"
    return "wait" if voices.get("busy") or voices.get("error") else "download-voices"


def _dbfs(samples, start, end):
    total = 0.0
    for index in range(start, end):
        value = samples[index]
        total += value * value
    mean = total / max(1, end - start)
    return -math.inf if mean <= 0 else 10 * math.log10(mean)


def speech_segments(samples):
    """`[(start, end)]` sample ranges that read as speech, linear in the take's length.

    A frame is active above ACTIVE_DBFS (the test recording's rule). Runs
    shorter than MIN_RUN_FRAMES are clicks, not speech; gaps up to
    MAX_GAP_FRAMES join runs so a phrase is not cut at every pause.
    """
    frames = len(samples) // FRAME
    segments, run_start, last_active = [], None, None
    for frame in range(frames):
        active = _dbfs(samples, frame * FRAME, (frame + 1) * FRAME) >= ACTIVE_DBFS
        if not active:
            continue
        if run_start is not None and frame - last_active - 1 > MAX_GAP_FRAMES:
            if last_active + 1 - run_start >= MIN_RUN_FRAMES:
                segments.append((run_start * FRAME, (last_active + 1) * FRAME))
            run_start = None
        if run_start is None:
            run_start = frame
        last_active = frame
    if run_start is not None and last_active + 1 - run_start >= MIN_RUN_FRAMES:
        segments.append((run_start * FRAME, (last_active + 1) * FRAME))
    return segments


def speech_ms(segments):
    return sum(end - start for start, end in segments) * 1000 // RATE


def enrol_windows(segments):
    """The windows CAM++ reads: D25 edge trim, minimum part and 10 s split, in order."""
    trim, shortest, longest = EDGE_TRIM_MS * RATE // 1000, MIN_SEGMENT_MS * RATE // 1000, MAX_WINDOW_MS * RATE // 1000
    windows = []
    for start, end in segments:
        start, end = start + trim, end - trim
        if end - start < shortest:
            continue
        parts = (end - start + longest - 1) // longest
        windows.extend(
            (start + (end - start) * i // parts, start + (end - start) * (i + 1) // parts) for i in range(parts)
        )
    return windows


def judge(samples):
    """`(windows, ok)`: ok when the trimmed speech reaches MIN_ENROL_MS."""
    windows = enrol_windows(speech_segments(samples))
    return windows, sum(end - start for start, end in windows) * 1000 // RATE >= MIN_ENROL_MS


def cache_root():
    """$XDG_CACHE_HOME/cassini-talk, as sound_check; a relative XDG_CACHE_HOME is ignored."""
    base = os.environ.get("XDG_CACHE_HOME")
    base = Path(base) if base and os.path.isabs(base) else Path.home() / ".cache"
    return base / "cassini-talk"


def samples_root():
    return cache_root() / "enrol"


@contextlib.contextmanager
def private_sample(samples, root=None):
    """Write the take to a private 0600 file for the CAM++ child; always delete it."""
    root = Path(root) if root is not None else samples_root()
    root.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    for level in (root.parent, root):
        if level.is_symlink():
            raise ValueError(f"The voice sample folder is a link: {level}")
        level.mkdir(mode=0o700, exist_ok=True)
    path = root / f"{uuid4()}.f32"
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            data = array("f", samples)
            if data.itemsize != 4:
                raise ValueError("Unexpected float size")
            if sys.byteorder != "little":
                data.byteswap()
            stream.write(data.tobytes())
        yield path
    finally:
        path.unlink(missing_ok=True)


def sweep(root=None):
    """Delete samples a crash left behind (only `<uuid>.f32` files directly in the folder)."""
    root = Path(root) if root is not None else samples_root()
    if not root.is_dir() or root.is_symlink():
        return
    for path in root.iterdir():
        with contextlib.suppress(ValueError):
            if path.suffix == ".f32" and UUID(path.stem) and not path.is_symlink():
                path.unlink(missing_ok=True)


def extract_print(app, samples, windows):
    """Worker-thread only: CAM++ print of the take's speech windows, or None."""
    models = app.voice_models
    if models is None:
        return None
    with private_sample(samples) as path:
        options = models.request(path.parent, origin="own")
        if options is None:
            return None
        from .transcription import bundle_path
        from .voice_models import VoiceJobClient

        with VoiceJobClient(bundle_path(), options["modelPath"], options["runtimePath"], timeout=120) as client:
            return client.enrol_sample(path, len(samples), windows, options)
