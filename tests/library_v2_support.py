# SPDX-License-Identifier: GPL-3.0-or-later
"""Fixtures for the Library v2 tests: v2 sessions with WAV spans, click markers, decoding."""

import json
import os
import shutil
import struct
import uuid
from pathlib import Path

import numpy as np

from cassini_viewer.library_v2 import journal, publish

RATE = 48000
CLICK_SECONDS = 0.004


def click(rate, channels=1, level=0.8):
    """A 4 ms Hann-windowed 3 kHz burst: a marker that survives Opus."""
    count = round(rate * CLICK_SECONDS)
    t = np.arange(count) / rate
    burst = level * np.sin(2 * np.pi * 3000 * t) * np.hanning(count)
    return np.repeat(burst[:, None], channels, axis=1) if channels > 1 else burst


def wav_header(frames, rate, channels):
    size = frames * channels * 2
    return struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + size,
        b"WAVE",
        b"fmt ",
        16,
        1,
        channels,
        rate,
        rate * channels * 2,
        channels * 2,
        16,
        b"data",
        size,
    )


def pcm16(samples):
    return np.clip(np.round(np.asarray(samples) * 32767), -32768, 32767).astype("<i2").tobytes()


def write_wav(path, samples, rate=RATE, channels=1):
    samples = np.asarray(samples, dtype=np.float64)
    frames = len(samples)
    Path(path).write_bytes(wav_header(frames, rate, channels) + pcm16(samples))
    return frames


def write_wav_blocks(path, frames, rate, block, channels=1):
    """Write `frames` frames produced by block(start, count) -> float array, one bounded block at a time."""
    with open(path, "wb") as stream:
        stream.write(wav_header(frames, rate, channels))
        for start in range(0, frames, rate * 10):
            count = min(rate * 10, frames - start)
            stream.write(pcm16(block(start, count)))
    return frames


def track_audio(rate, seconds, clicks=(), silent=(), tone=0.02, channels=1):
    """`seconds` of a quiet tone with click markers at `clicks` (s), zero in the `silent` (start, end) ranges."""
    count = round(rate * seconds)
    t = np.arange(count) / rate
    audio = tone * np.sin(2 * np.pi * 220 * t)
    for start, end in silent:
        audio[round(start * rate) : round(end * rate)] = 0
    for at in clicks:
        burst = click(rate)
        first = round(at * rate) - len(burst) // 2
        audio[first : first + len(burst)] += burst
    return np.repeat(audio[:, None], channels, axis=1) if channels > 1 else audio


def new_session(root, tracks, *, title="Weekly sync", created="2026-10-30T17:02:41Z", offset=60, meeting=None):
    """A stopped v2 recording session: `tracks` are session.json track dicts, files written by the caller.

    Returns (session_id, directory). Call with files=... in each track to write them here.
    """
    layout = journal.Layout(Path(root))
    layout.ensure()
    session_id = str(uuid.uuid4())
    directory = layout.session("Recording", session_id)
    directory.mkdir(mode=0o700)
    manifest = dict(
        version=1, id=session_id, createdAt=created, title=title, source="native", state="completed", tracks=[]
    )
    if offset is not None:
        manifest["startedAtUtcOffsetMinutes"] = offset
    for track in tracks:
        track = dict(track)
        spans = track.pop("write", None)
        for name, (samples, rate, channels) in (spans or {}).items():
            write_wav(directory / name, samples, rate, channels)
        manifest["tracks"].append(track)
    (directory / "session.json").write_text(json.dumps(manifest))
    if meeting is not None:
        (directory / "meeting.json").write_text(json.dumps(dict(meeting, id=session_id)))
    return session_id, directory


def simple_session(root, seconds=1.0, *, title="Weekly sync", created="2026-10-30T17:02:41Z", **kw):
    audio = track_audio(RATE, seconds, clicks=[seconds / 2])
    track = dict(
        id="microphone",
        label="Microphone",
        kind="microphone",
        file="track-microphone.wav",
        sampleRate=RATE,
        channels=1,
        frames=len(audio),
        startOffsetMs=0,
        write={"track-microphone.wav": (audio, RATE, 1)},
    )
    return new_session(root, [track], title=title, created=created, **kw)


def meeting_json(directory):
    path = Path(directory) / "meeting.json"
    return json.loads(path.read_text()) if path.exists() else None


def root_files(root):
    """What the §1 scanner lists: regular *.opus directly in the root, no dotfiles."""
    return sorted(
        n
        for n in os.listdir(root)
        if n.endswith(".opus")
        and not n.startswith(".")
        and (Path(root) / n).is_file()
        and not (Path(root) / n).is_symlink()
    )


def rows(root):
    """{(origin, sessionId): [row kinds]}: one row per published file plus one per unpublished session."""
    layout = journal.Layout(Path(root))
    found = {}
    for name in root_files(root):
        identity = publish.file_identity(layout.root / name)
        found.setdefault(identity[:2] if identity else ("file", name), []).append(("file", name, identity[2]))
    if layout.sessions.is_dir():
        for name in os.listdir(layout.sessions):
            identity = journal.identity_of(name)
            meeting = meeting_json(layout.sessions / name) or {}
            if identity and meeting.get("published") is not True:
                found.setdefault(identity, []).append(("session", name, meeting.get("revision", 0)))
    return found


def decoder_for(path, scratch):
    """track_source.OpusDecoder for a published file (it only opens confined `tracks/` spans)."""
    from cassini_viewer.track_source import OpusDecoder

    folder = Path(scratch) / "decode" / "tracks"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / "published.opus"
    shutil.copyfile(path, target)
    return OpusDecoder(target)


def mono_window(decoder, start, count):
    decoder.seek(max(0, start))
    data = decoder.read(count)
    values = np.frombuffer(data, dtype="<i2").astype(np.float64).reshape(-1, decoder.channels).mean(axis=1) / 32768
    return values


def marker_at(decoder, expected, search=RATE // 2):
    """Sample index of the strongest click within `search` samples of `expected`."""
    start = max(0, expected - search)
    window = mono_window(decoder, start, 2 * search)
    energy = np.convolve(window**2, np.ones(96), mode="same")
    return start + int(np.argmax(energy))


def rms(decoder, start, count):
    window = mono_window(decoder, start, count)
    return float(np.sqrt(np.mean(window**2))) if len(window) else 0.0
