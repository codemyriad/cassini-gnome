#!/usr/bin/env python3
"""Materialize the shared placement golden (shared test 9): tone-burst sessions.

    python3 conformance/placement/make.py OUT
    python3 conformance/placement/make.py --mutant NAME OUT   # one offset moved (bursts.json "mutants")

Every burst is 250 ms of a 1 kHz square wave of +-8000 that starts with
+8000 on its first sample; every other sample is exactly 0, so a burst's
onset is its first non-zero sample. Each track holds six bursts, 2000 ms apart,
in 12 s: 1.5 s of tone, which D56's 32-frame rule counts as speech, not silence.
Stereo tracks carry the same samples on both channels. Integer samples only,
stdlib only: Linux and macOS write identical bytes (materialized.sha256).
"""
import argparse
from array import array
import json
from pathlib import Path
import sys
import wave

ROOT = Path(__file__).resolve().parent
LEVEL = 8000
BURST_MS = 250
TONE_HZ = 1000


def burst(rate):
    """One burst at rate: +LEVEL for the first half-period, then -LEVEL, ..."""
    return [LEVEL if (i * 2 * TONE_HZ // rate) % 2 == 0 else -LEVEL for i in range(BURST_MS * rate // 1000)]


def samples(track):
    """A track's mono PCM16 samples: silence with its bursts at their local ms."""
    rate = track['sampleRate']
    out = array('h', bytes(2 * track['lengthMs'] * rate // 1000))
    tone = burst(rate)
    for local in track['burstsMs']:
        first = local * rate // 1000
        assert local * rate % 1000 == 0 and first + len(tone) <= len(out), (track['id'], local)
        out[first:first + len(tone)] = array('h', tone)
    return out


def document(session):
    """The session.json the recorder would have written for this case."""
    tracks = []
    for track in session['tracks']:
        offset = track['startOffsetMs']
        tracks.append({
            'id': track['id'],
            'label': track['label'],
            'kind': track['kind'],
            'file': f"track-{track['id']}.wav",
            'sampleRate': track['sampleRate'],
            'channels': track['channels'],
            'startOffsetMs': offset,
            'endOffsetMs': None if offset is None else offset + track['lengthMs'],
            'frames': track['lengthMs'] * track['sampleRate'] // 1000,
            'ended': True,
            'labelSource': 'device',
        })
    return {
        'version': 1,
        'id': session['id'],
        'createdAt': session['createdAt'],
        'title': session['title'],
        'source': session['source'],
        'state': 'completed',
        'tracks': tracks,
    }


def sessions(mutant=None):
    """bursts.json's sessions; with a mutant name, its one track offset moved."""
    golden = json.loads((ROOT / 'bursts.json').read_text())
    if mutant is not None:
        change = next(m for m in golden['mutants'] if m['name'] == mutant)
        session = next(s for s in golden['sessions'] if s['id'] == change['session'])
        track = next(t for t in session['tracks'] if t['id'] == change['track'])
        track['startOffsetMs'] = change['startOffsetMs']
    return golden['sessions']


def make(destination, mutant=None):
    for session in sessions(mutant):
        root = destination / ('Recording-' + session['id'])
        root.mkdir(parents=True, exist_ok=False)
        for track in session['tracks']:
            mono = samples(track)
            if track['channels'] == 2:
                frames = array('h', bytes(4 * len(mono)))
                frames[0::2] = mono
                frames[1::2] = mono
            else:
                frames = mono
            if sys.byteorder != 'little':
                frames.byteswap()
            with wave.open(str(root / f"track-{track['id']}.wav"), 'wb') as writer:
                writer.setnchannels(track['channels'])
                writer.setsampwidth(2)
                writer.setframerate(track['sampleRate'])
                writer.writeframes(frames.tobytes())
        (root / 'session.json').write_text(json.dumps(document(session), indent=2) + '\n')
        print(root)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--mutant', help='move one offset as bursts.json "mutants" says (app mutant runs)')
    parser.add_argument('destination', type=Path)
    args = parser.parse_args()
    make(args.destination, args.mutant)
