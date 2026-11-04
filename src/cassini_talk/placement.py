# SPDX-License-Identifier: GPL-3.0-or-later
"""D55 (M9): the one placement of a track on the session timeline.

Every consumer places a track that starts at ``startOffsetMs`` at session frame
``startOffsetMs * rate / 1000``, rounded half away from zero, at its own rate:
16 kHz for recognition (words, rejoin, presence), 48 kHz for the export and
publication mixes, the track's own rate for folder playback. Transcription and
export place an unknown (None) offset at an assumed 0 and flag it
(``offsetsAssumedZero``, D19); playback refuses it. Those policies stay with
the callers; this module only rounds.

The arithmetic is IEEE binary64 in Swift's order, ``ms * rate / 1000``, then
``.toNearestOrAwayFromZero`` on that double, so both apps agree on every
offset (Mac: TrackPlacement.frame). ``floor(x + 0.5)`` is not that rounding:
the addition itself rounds, so 0.49999999999999994 would become 1.
"""

import math

WORD_RATE = 16000


def frame(ms, rate):
    """The session frame at ``rate`` of a track starting at ``ms`` (D55)."""
    x = ms * rate / 1000
    if not math.isfinite(x):
        raise ValueError("A track offset must be a finite number of milliseconds")
    magnitude = abs(x)
    whole = math.floor(magnitude)
    # magnitude - whole is exact for every finite double, so this is the true tie test.
    placed = whole + (magnitude - whole >= 0.5)
    return -placed if x < 0 else placed


def word_ms(offset_ms, sample):
    """Session ms of the track's 16 kHz recognizer sample ``sample`` (D55, Mac MeetingTranscriber).

    The track's first sample is at ``frame(offset_ms, 16000)``; a session sample
    maps to whole ms by flooring, so a word time never precedes its audio.
    """
    return (frame(offset_ms, WORD_RATE) + sample) * 1000 // WORD_RATE
