#!/usr/bin/env python3
"""Independent oracle for the placement golden (shared test 9, D55).

    placement.py expected            print what bursts.json's "expected" must be
    placement.py check RESULTS.json  check one app's results against bursts.json
    placement.py compare A.json B.json   per-burst deltas between two apps

D55: a track that starts at offsetMs is placed at frame
offsetMs * rate / 1000 rounded half away from zero, at each consumer's rate:
16 kHz for recognition, 48 kHz for the export mix, the track's own rate for
playback. Transcription and export place an unknown (null) offset at an
assumed 0 and flag it (offsetsAssumedZero); playback refuses the whole session
(D19). Arithmetic here is exact (Fraction of the offset's binary double, the
value every app parses).

    placement.py check --mutant NAME RESULTS.json
        results measured on `make.py --mutant NAME`: they must fail exactly the
        legs bursts.json names for that mutant, and only in its session.
"""
import argparse
from fractions import Fraction
import json
import math
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
HEADLINE_MS = 20
LEGS = ('rec16', 'wordMs', 'mix48', 'playback')  # required in every app's results
OPTIONAL = ('opus48', 'mix48publication')  # mix48publication: an app's second 48 kHz mix
FLAGS = 'offsetsAssumedZero'  # required: per session, {"transcript": bool, "export": bool}


def frame(ms, rate):
    """D55: ms * rate / 1000, rounded half away from zero (ms >= 0)."""
    x = Fraction(ms) * rate / 1000
    whole = math.floor(x)
    return whole + (x - whole >= Fraction(1, 2))


def expected(golden):
    """Every burst's expected position per leg, from the sessions alone."""
    out = {}
    for session in golden['sessions']:
        # D19: playback refuses the whole session when any track's offset is unknown.
        refused = any(t['startOffsetMs'] is None for t in session['tracks'])
        for track in session['tracks']:
            rate, offset = track['sampleRate'], track['startOffsetMs']
            assumed = offset is None
            at = 0 if assumed else offset
            local = track['burstsMs']
            rec16 = [frame(at, 16000) + ms * 16 for ms in local]
            mix48 = [frame(at, 48000) + ms * 48 for ms in local]
            entry = {
                'offsetsAssumedZero': assumed,
                # The shared FIR keeps a 48 kHz burst's onset on its 16 kHz sample: exact.
                'rec16': {'tolerance': 0, 'frames': rec16},
                'wordMs': {'tolerance': 0, 'values': [f * 1000 // 16000 for f in rec16]},
                # A 16 kHz track is resampled into the 48 kHz mix: only the headline holds.
                'mix48': {'tolerance': 0 if rate == 48000 else 48 * HEADLINE_MS, 'frames': mix48},
                'playback': 'refused' if refused else {
                    'rate': rate, 'tolerance': 0 if rate == 48000 else rate * HEADLINE_MS // 1000,
                    'frames': [frame(at, rate) + ms * rate // 1000 for ms in local]},
                'opus48': {'tolerance': 48 * HEADLINE_MS, 'frames': mix48},
            }
            out[f"{session['id']}/{track['id']}"] = entry
    return out


def flags(golden):
    """Per session: transcription and export both flag offsetsAssumedZero (D55 M9)."""
    return {s['id']: any(t['startOffsetMs'] is None for t in s['tracks']) for s in golden['sessions']}


def onset(stream, rate, at):
    """The shared onset detector. stream is mono (stereo averaged), at the
    expected sample: in [at - 0.05 rate, at + 0.2 rate), the first sample whose
    magnitude reaches half the window's peak; None if the window is silent."""
    lo, hi = max(0, at - rate // 20), min(len(stream), at + rate // 5)
    peak = max((abs(v) for v in stream[lo:hi]), default=0)
    if not peak:
        return None
    return next(n for n in range(lo, hi) if 2 * abs(stream[n]) >= peak)


def strict(text):
    """Parse a results file: no NaN or Infinity, no repeated keys."""
    def pairs(items):
        keys = [k for k, _ in items]
        if len(set(keys)) != len(keys):
            raise ValueError(f'repeated key in {keys}')
        return dict(items)

    def constant(name):
        raise ValueError(f'{name} is not a position')

    return json.loads(text, object_pairs_hook=pairs, parse_constant=constant)


def _position(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _findings(results, golden):
    """(leg, key, message) for everything the results get wrong."""
    literals = json.loads((ROOT / 'placement.json').read_text())['cases']
    if results.get('vector') != golden['vector']:
        yield None, None, f"vector is {results.get('vector')!r}, not {golden['vector']!r}"
    legs = results.get('legs', {})
    for leg in sorted(set(legs) - {'placement', FLAGS} - set(LEGS + OPTIONAL)):
        yield leg, None, f'{leg}: not a leg of {golden["vector"]}'
    cases = [(c['ms'], c['rate'], c['frame']) for c in literals]
    got = legs.get('placement', [])
    if not isinstance(got, list) or any(not isinstance(c, list) or len(c) != 3 for c in got) \
            or [tuple(c[:2]) for c in got] != [c[:2] for c in cases]:
        yield 'placement', None, 'placement: the results do not list placement.json cases in order as [ms, rate, frame]'
    else:
        for (ms, rate, f), (_, _, want) in zip(got, cases):
            if not _position(f) or f != want:
                yield 'placement', None, f'placement: {ms} ms at {rate} Hz is frame {f!r}, not {want}'
    want_flags, have_flags = flags(golden), legs.get(FLAGS)
    if not isinstance(have_flags, dict):
        yield FLAGS, None, f'{FLAGS}: missing'
    else:
        for session, want in want_flags.items():
            have = have_flags.get(session)
            for where in ('transcript', 'export'):
                value = have.get(where) if isinstance(have, dict) else None
                if value is not want:
                    yield FLAGS, session, f'{FLAGS} {session} {where}: {value!r}, not {want}'
        for session in sorted(set(have_flags) - set(want_flags)):
            yield FLAGS, session, f'{FLAGS} {session}: not a session of {golden["vector"]}'
    for leg in LEGS + OPTIONAL:
        if leg not in legs:
            if leg in LEGS:
                yield leg, None, f'{leg}: missing'
            continue
        if not isinstance(legs[leg], dict):
            yield leg, None, f'{leg}: not an object of tracks'
            continue
        for key in sorted(set(legs[leg]) - set(golden['expected'])):
            yield leg, key, f'{leg} {key}: not a track of {golden["vector"]}'
        source = 'mix48' if leg == 'mix48publication' else leg
        for key, entry in golden['expected'].items():
            want, have = entry[source], legs[leg].get(key)
            if want == 'refused' or have == 'refused':
                if want != have:
                    yield leg, key, f'{leg} {key}: {have!r}, not {want!r}'
                continue
            values = want.get('frames', want.get('values'))
            if not isinstance(have, list) or len(have) != len(values):
                yield leg, key, f'{leg} {key}: expected {len(values)} bursts, got {have!r}'
                continue
            rate = 1000 if leg == 'wordMs' else want.get('rate', 16000 if leg == 'rec16' else 48000)
            for index, (f, w) in enumerate(zip(have, values)):
                if f is None:
                    yield leg, key, f'{leg} {key} burst {index}: not found'
                elif not _position(f):
                    yield leg, key, f'{leg} {key} burst {index}: {f!r} is not a whole sample'
                elif abs(f - w) * 1000 > HEADLINE_MS * rate:
                    yield leg, key, f'{leg} {key} burst {index}: {f}, more than {HEADLINE_MS} ms from {w}'
                elif abs(f - w) > want['tolerance']:
                    yield leg, key, f"{leg} {key} burst {index}: {f}, not {w} (exact tier, +-{want['tolerance']})"


def golden_json():
    return json.loads((ROOT / 'bursts.json').read_text())


def problems(results, golden=None):
    """What an app's results file gets wrong; empty when it passes."""
    return [message for _, _, message in _findings(results, golden or golden_json())]


def mutant_problems(name, results, golden=None):
    """Results measured on `make.py --mutant NAME` against the unchanged expectations:
    every named leg the results carry must fail in the mutant's session, and nothing else."""
    golden = golden or golden_json()
    mutant = next((m for m in golden.get('mutants', []) if m['name'] == name), None)
    if mutant is None:
        return [f'no mutant {name!r}']
    session = mutant['session']
    failed, found = set(), []
    for leg, key, message in _findings(results, golden):
        if leg in mutant['fails'] and key is not None and key.split('/')[0] == session:
            failed.add(leg)
        else:
            found.append(f'{name}: unexpected: {message}')
    legs = results.get('legs', {})
    found += [f'{name}: {leg} passes; this mutant must fail it'
              for leg in mutant['fails'] if leg in legs and leg not in failed]
    return found


def compare(a, b):
    """Per-burst differences between two apps' results (coordinator QA)."""
    lines = []
    for leg in sorted(set(a.get('legs', {})) | set(b.get('legs', {}))):
        left, right = a['legs'].get(leg), b['legs'].get(leg)
        if leg == 'placement' or left is None or right is None:
            if left != right:
                lines.append(f"{leg}: {a.get('app')} {left!r} vs {b.get('app')} {right!r}")
            continue
        for key in sorted(set(left) | set(right)):
            if left.get(key) != right.get(key):
                lines.append(f"{leg} {key}: {a.get('app')} {left.get(key)!r} vs {b.get('app')} {right.get(key)!r}")
    return lines


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('expected')
    check = sub.add_parser('check')
    check.add_argument('--mutant', help='the results were measured on make.py --mutant MUTANT')
    check.add_argument('results', type=Path)
    pair = sub.add_parser('compare')
    pair.add_argument('a', type=Path)
    pair.add_argument('b', type=Path)
    args = parser.parse_args(argv)
    if args.command == 'expected':
        print(json.dumps(expected(golden_json()), indent=2))
        return 0
    if args.command == 'check':
        try:
            results = strict(args.results.read_text())
        except ValueError as error:
            print(f'{args.results}: {error}')
            return 1
        if args.mutant:
            found = mutant_problems(args.mutant, results)
            print('\n'.join(found) or f'{args.results}: {args.mutant} fails exactly where it must')
        else:
            found = problems(results)
            print('\n'.join(found) or f'{args.results}: every burst placed as D55 says')
        return 1 if found else 0
    lines = compare(strict(args.a.read_text()), strict(args.b.read_text()))
    print('\n'.join(lines) or 'identical')
    return 1 if lines else 0


if __name__ == '__main__':
    sys.exit(main())
