#!/usr/bin/env python3
"""Materialize D52's pinned public-speech corpus, no devices/network/wall waits.

16 kHz originals are repeated three times per sample, attenuated by 1/2 and
placed at 48 kHz. This is a fixture carrier, not a recognition resampler.
A speech window [a, b, phaseMs] plays its voice from position (t - phaseMs)
at session time t; phaseMs defaults to the track start. Two tracks that carry
one call share a phase, so a physical overlap is byte-identical in both.
A bleed copies another track's own signal (speech and noise, never its bleeds)
at `db`. A noise bed is xorshift32 seeded by `seed`, held like the speech and
indexed from `originMs`, so tracks that name one bed carry the same samples.
Gap ranges follow D52.2 (P7); presenceWindows only mask the generator.
Stdlib only, integer positions and correctly rounded gains: Linux and macOS
produce identical bytes.
"""
import argparse
from array import array
from decimal import Decimal
import hashlib
import json
from operator import add
from pathlib import Path
import sys
import wave

ROOT = Path(__file__).resolve().parent
RATE = 48000
PER_MS = RATE // 1000
HOLD = 3  # 16 kHz carrier samples per 48 kHz frame


def load():
    corpus = json.loads((ROOT / 'corpus.json').read_text())
    speech = {}
    for key, item in corpus['speech'].items():
        path = ROOT / item['file']
        assert hashlib.sha256(path.read_bytes()).hexdigest() == item['sha256'], path
        with wave.open(str(path)) as reader:
            assert (reader.getframerate(), reader.getnchannels(), reader.getsampwidth()) == (16000, 1, 2)
            samples = array('h', reader.readframes(reader.getnframes()))
            if sys.byteorder != 'little':
                samples.byteswap()
            speech[key] = samples
    return corpus, speech


def gain(db):
    """10^(db/20), correctly rounded (Decimal), so no platform libm is involved."""
    return float(Decimal(10) ** (Decimal(db) / Decimal(20)))


def amplitude(dbfs):
    """Uniform noise of RMS 32768*10^(dbfs/20) spans +-sqrt(3) times that RMS."""
    return float(Decimal(32768) * Decimal(3).sqrt() * Decimal(10) ** (Decimal(dbfs) / Decimal(20)))


def windows(track, key):
    """[a, b] or [a, b, phaseMs] entries as (a, b, phaseMs)."""
    return [(w[0], w[1], w[2] if len(w) > 2 else track['startMs']) for w in track.get(key, [])]


def presence(track):
    return [tuple(w) for w in track.get('presenceWindows', [(track['startMs'], track['endMs'])])]


def clip(spans, mask):
    for a, b, *rest in spans:
        for c, d in mask:
            left, right = max(a, c), min(b, d)
            if left < right:
                yield (left, right, *rest)


class Noise:
    def __init__(self):
        self.streams = {}

    def held(self, seed, origin, first, last):
        """16 kHz uniform words [first, last) of the bed `seed`, counted from origin."""
        assert 0 <= first <= last, ('noise window before its originMs', seed, origin, first, last)
        stream = self.streams.setdefault((seed, origin), [])
        state = stream[-1] if stream else int.from_bytes(hashlib.sha256(seed.encode()).digest()[:4], 'little') or 1
        while len(stream) < last:
            state ^= (state << 13) & 0xFFFFFFFF
            state ^= state >> 17
            state ^= (state << 5) & 0xFFFFFFFF
            stream.append(state)
        return stream[first:last]


def direct(track, left, right, speech, noise):
    """A track's own signal over [left, right) ms as unrounded 48 kHz floats."""
    out = [0.0] * ((right - left) * PER_MS)
    mask = presence(track)
    for a, b, phase in clip(windows(track, 'speechWindows'), mask):
        a, b = max(a, left), min(b, right)
        if a >= b:
            continue
        source = speech[track['speech']]
        size = len(source)
        held = [source[q % size] / 2 for q in range((a - phase) * 16, (b - phase) * 16) for _ in range(HOLD)]
        first = (a - left) * PER_MS
        out[first:first + len(held)] = map(add, out[first:first + len(held)], held)
    bed = track.get('noise')
    if bed:
        scale = amplitude(bed['dbfs']) / 2 ** 31
        for a, b in clip(bed['windows'], mask):
            a, b = max(a, left), min(b, right)
            if a >= b:
                continue
            words = noise.held(bed['seed'], bed['originMs'], (a - bed['originMs']) * 16, (b - bed['originMs']) * 16)
            held = [(word - 2 ** 31) * scale for word in words for _ in range(HOLD)]
            first = (a - left) * PER_MS
            out[first:first + len(held)] = map(add, out[first:first + len(held)], held)
    return out


def render(track, tracks, speech, noise):
    """Stored PCM16 for one track: its own signal plus bleeds, rounded half-even."""
    start, end = track['startMs'], track['endMs']
    out = direct(track, start, end, speech, noise)
    touched = [(a, b) for a, b, _ in clip(windows(track, 'speechWindows'), presence(track))]
    if track.get('noise'):
        touched += list(clip(track['noise']['windows'], presence(track)))
    for bleed in track.get('bleeds', []):
        other = tracks[bleed['from']]
        factor = gain(bleed['db'])
        for a, b in clip(bleed['windows'], presence(track)):
            a, b = max(a, start), min(b, end)
            if a >= b:
                continue
            first = (a - start) * PER_MS
            carried = [value * factor for value in direct(other, a, b, speech, noise)]
            out[first:first + len(carried)] = map(add, out[first:first + len(carried)], carried)
            touched.append((a, b))
    pcm = array('h', bytes(2 * (end - start) * PER_MS))
    for a, b in touched:
        a, b = max(a, start), min(b, end)
        first, last = (a - start) * PER_MS, (b - start) * PER_MS
        pcm[first:last] = array('h', [max(-32768, min(32767, round(v))) for v in out[first:last]])
    if sys.byteorder != 'little':
        pcm.byteswap()
    return pcm


def metadata(case):
    tracks = []
    for track in case['tracks']:
        frames = (track['endMs'] - track['startMs']) * PER_MS
        browser = 'sourceTrackId' in track
        value = dict(id=track['id'], kind=track['kind'], label='Guest' if browser else track['kind'],
                     labelSource='browser' if browser else 'device', sampleRate=RATE, channels=1,
                     file='track-' + track['id'] + '.wav', frames=frames, startOffsetMs=track['startMs'],
                     endOffsetMs=track['endMs'], ended=True)
        if browser:
            value['sourceTrackId'] = track['sourceTrackId']
        # D52.2: a new writer always records gaps; [] means none. Prefixes are not gapFrames.
        value['gaps'] = track.get('gaps', [])
        value['gapFrames'] = sum(g['frames'] for g in value['gaps'] if g['kind'] != 'prefix')
        if 'droppedFrames' in track:
            # Producer count only: never a range, never state (SPEC "Gap ranges").
            value['droppedFrames'] = track['droppedFrames']
        if browser:
            # Recorded samples count as received; inserted padding does not.
            value['receivedFrames'] = frames - value['gapFrames']
            value['captureOutcome'] = outcome(value['receivedFrames'] * 1000 // RATE)
        tracks.append(value)
    document = dict(version=1, id=case['id'], createdAt=case['createdAt'], title=case['title'], source='chromium',
                    state='completed', tracks=tracks, events=case['events'])
    sources = {}
    for value in tracks:
        if 'sourceTrackId' in value:
            sources.setdefault(value['sourceTrackId'], []).append(value)
    if sources:
        document['captureOutcome'] = dict(sources=[
            dict(sourceTrackId=key, kind=spans[0]['kind'], label=spans[0]['label'],
                 captureOutcome=outcome(sum(t['receivedFrames'] for t in spans) * 1000 // RATE),
                 audioMs=sum(t['receivedFrames'] for t in spans) * 1000 // RATE, trackIds=[t['id'] for t in spans])
            for key, spans in sources.items()])
    long = [t for t in tracks if any(g['kind'] != 'prefix' and g['frames'] >= t['sampleRate'] for g in t['gaps'])]
    if long:
        document.update(state='interrupted', problemKind='gap', problem=' '.join(
            f"{t['gapFrames']*1000//t['sampleRate']} ms of {t['kind']} input time was preserved as silence." for t in long))
    return document


def outcome(milliseconds):
    return 'neverAudio' if not milliseconds else 'lowAudio' if milliseconds < 1000 else 'captured'


def make(destination):
    corpus, speech = load()
    noise = Noise()
    for case in corpus['sessions']:
        root = destination / ('Recording-' + case['id'])
        root.mkdir(parents=True, exist_ok=False)
        document = metadata(case)
        tracks = {track['id']: track for track in case['tracks']}
        for track, stored in zip(case['tracks'], document['tracks']):
            with wave.open(str(root / stored['file']), 'wb') as writer:
                writer.setnchannels(1)
                writer.setsampwidth(2)
                writer.setframerate(RATE)
                writer.writeframesraw(render(track, tracks, speech, noise).tobytes())
        (root / 'session.json').write_text(json.dumps(document, indent=2) + '\n')
        print(root)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination', type=Path)
    make(parser.parse_args().destination)
