# SPDX-License-Identifier: GPL-3.0-or-later
"""D56 (R75 Q1): tracks left out of recognition and speaker separation.

D56's rule: a track with fewer than 32 non-zero 32 ms frames is silent, a
frame being non-zero when any sample != 0 (constant DC counts as present).
GNOME's exact geometry, which Mac must match:

- A frame is floor(rate * 32 / 1000) sample frames of one physical file at
  the rate it is stored (WAV) or decoded (compacted Opus: 48 kHz), so 1536 at
  48 kHz and 512 at 16 kHz. There is no downmix and no resampling.
- Frames do not overlap and start at each file's first sample. A trailing
  partial frame (fewer samples than a frame) is not counted.
- A frame is non-zero when any sample of any channel is != 0.
- The verdict is per track entry (one session.json track; each span of a
  source is its own entry). A rotated entry's files are framed each from
  their own first sample and their counts are summed.

This is not gocassini's envelope geometry: its frameStats sees a 16 kHz mono
downmix in overlapping 32 ms windows at a 16 ms hop. Only the "any sample
!= 0" test is shared.

Silent tracks stay in the published mixdown and their captureOutcome is
unchanged; only the writing-down leaves them out. The count stops at 32, so a
track with sound costs about one second of reading. Only a silent track is
read to its end, and `check` runs before every block so a cancelled job stops
mid-file. No NumPy: a PCM16 sample is zero exactly when both of its
bytes are, so frames are compared as bytes.
"""

import logging
import os

from . import track_source

LOG = logging.getLogger(__name__)

FRAME_MS = 32
MIN_FRAMES = 32
# Frames read per block; whole frames, so only the file's end can be partial.
BLOCK_FRAMES = 256


def frame_samples(rate):
    """Sample frames in one 32 ms frame at this rate, rounded down."""
    return rate * FRAME_MS // 1000


def _unchecked():
    pass


def nonzero_frames(blocks, frame_bytes, limit=MIN_FRAMES, check=_unchecked):
    """Non-zero frames in interleaved PCM16 bytes, stopping once `limit` are found.

    `blocks` may split frames anywhere. Bytes left over at the end, fewer than a
    frame, are a partial frame and never count. `check()` runs before each
    block is read; whatever it raises (CancelledError) stops the count.
    """
    if frame_bytes <= 0:
        raise ValueError("Invalid frame size")
    zero = bytes(frame_bytes)
    found = 0
    pending = b""
    blocks = iter(blocks)
    while True:
        check()
        if (block := next(blocks, None)) is None:
            break
        data = pending + bytes(block)
        whole = len(data) - len(data) % frame_bytes
        if data.count(0, 0, whole) != whole:
            view = memoryview(data)
            for start in range(0, whole, frame_bytes):
                if view[start : start + frame_bytes] != zero:
                    found += 1
                    if found >= limit:
                        return found
        pending = data[whole:]
    return found


def _wav_frames(path, limit, check):
    fd = track_source.open_track(path, "pcm")
    with os.fdopen(fd, "rb") as stream:
        # The header read now, not the scan, sets the rate and channels.
        frames, rate, channels = track_source.wave_header(stream, os.fstat(stream.fileno()))
        frame_bytes = frame_samples(rate) * channels * 2

        def blocks():
            left = frames * channels * 2
            while left > 0 and (block := stream.read(min(left, frame_bytes * BLOCK_FRAMES))):
                left -= len(block)
                yield block

        return nonzero_frames(blocks(), frame_bytes, limit, check)


def _opus_frames(path, limit, check):
    decoder = track_source.OpusDecoder(path)
    try:
        size = frame_samples(track_source.OPUS_RATE)

        def blocks():
            while block := decoder.read(size * BLOCK_FRAMES):
                yield block

        return nonzero_frames(blocks(), size * decoder.channels * 2, limit, check)
    finally:
        decoder.close()


def silent(track, check=_unchecked):
    """True when a scanned CaptureTrack has fewer than 32 non-zero 32 ms frames.

    A file that cannot be read keeps its track: the writing-down then meets the
    same error it always did, rather than hiding the track as silent. `check`
    is the job's cancellation check; what it raises propagates.
    """
    found = 0
    try:
        for span in track.spans or (track,):
            check()
            measure = _opus_frames if span.codec == "opus" else _wav_frames
            found += measure(span.path, MIN_FRAMES - found, check)
            if found >= MIN_FRAMES:
                return False
    except (ImportError, OSError, ValueError, TimeoutError) as error:
        LOG.warning("Could not measure track %s for silence; keeping it: %s", track.path.name, error)
        return False
    return True
