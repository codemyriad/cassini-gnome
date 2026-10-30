#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Check S-6 metadata/reader fixtures; this does not test writer crash durability."""
import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile
import wave
from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / 'conformance/session-events'
SCHEMA = Draft202012Validator(json.loads((ROOT / 'schemas/session.schema.json').read_text()),
                             format_checker=FormatChecker())


def validate(document):
    """JSON Schema plus S-6 bounds that JSON Schema cannot express."""
    SCHEMA.validate(document)
    previous = 0
    ended = False
    source = None
    others = True
    for event in document.get('events') or []:
        at = event['atMs']
        if not math.isfinite(at) or at < previous:
            raise ValueError('events must have finite non-decreasing atMs')
        if ended:
            raise ValueError('end is terminal; no event may follow it')
        if event['type'] == 'selection':
            others = event['others']
        elif event['type'] == 'othersSource':
            if event['reason'] == 'restored' and (not others or source is None or
                    source['source'] != 'system' or source['reason'] not in ('missing', 'lost')):
                raise ValueError('restored needs an enabled others side and a preceding fallback')
            source = event
        elif event['type'] == 'end':
            ended = True
            if document['state'] == 'recording':
                raise ValueError('end must be checkpointed with saved state')
            for track in document['tracks']:
                end = track.get('endOffsetMs')
                if end is not None and (not math.isfinite(end) or at < end):
                    raise ValueError('end must not precede a finalized known track end')
        previous = at
    for track in document['tracks']:
        source = track.get('sourceTrackId')
        if source is not None and len(source.encode('utf-8')) > 200:
            raise ValueError('sourceTrackId exceeds 200 UTF-8 bytes')


def state_at(document, at):
    """Independent fixture oracle, not a runtime reader used by either app."""
    selection = source = None
    for event in document.get('events') or []:
        if event['atMs'] > at:
            break
        if event['type'] == 'selection':
            selection = event
        elif event['type'] == 'othersSource':
            source = event
    if selection is None or source is None:
        return None  # No fallback/source/selection history is inferred.
    return dict(atMs=at, me=selection['me'], others=selection['others'], source=source['source'],
                reason=source['reason'], sinceMs=source['atMs'],
                fallback=source['reason'] in ('missing', 'lost'))


def check_fixtures():
    for case, manifest_name, expected_name in [
        ('fallback', 'session.json', 'expected.json'),
        ('browser-off-on', 'browser-off-on.session.json', 'browser-off-on.expected.json'),
        ('call-return', 'call-return.session.json', 'call-return.expected.json'),
    ]:
        document = json.loads((FIXTURES / manifest_name).read_text())
        expected = json.loads((FIXTURES / expected_name).read_text())
        validate(document)
        duration = max(track['endOffsetMs'] for track in document['tracks'])
        assert duration == expected['durationMs']
        for state in expected.get('states', []):
            assert state_at(document, state['atMs']) == state
        if 'trackSpeakers' in expected:
            speakers = {track['file']: ('me' if track['kind'] in ('microphone', 'local') else
                        'others' if track['kind'] == 'system' else
                        'track-' + (track.get('sourceTrackId') or track['id'])) for track in document['tracks']}
            assert speakers == expected['trackSpeakers']
            assert len(set(speakers.values())) == expected['speakerCount']
            assert {track['file']: track['startOffsetMs'] for track in document['tracks']} == expected['trackOffsetsMs']
            final = state_at(document, duration)
            for key in ('me', 'others', 'source', 'fallback'):
                assert final[key] == expected[key]
        if 'end' in expected:
            assert document['events'][-1] == dict(type='end', **expected['end'])
        if 'trackRoles' in expected:
            roles = {track['id']: 'me' if track['kind'] in ('microphone', 'local') else 'others' for track in document['tracks']}
            assert roles == expected['trackRoles']
            assert {track['id']: track['endOffsetMs'] for track in document['tracks']} == expected['trackEndsMs']
        with tempfile.TemporaryDirectory(prefix='cassini-s6-') as temp:
            result = subprocess.check_output([sys.executable, str(FIXTURES / 'make.py'), temp, '--case', case], text=True)
            folder = Path(result.strip())
            assert json.loads((folder / 'session.json').read_text()) == document
            for track in document['tracks']:
                with wave.open(str(folder / track['file'])) as audio:
                    assert (audio.getnchannels(), audio.getsampwidth(), audio.getframerate(), audio.getnframes()) == (
                        track['channels'], 2, track['sampleRate'], track['frames'])
                assert track['endOffsetMs'] == track['startOffsetMs'] + track['frames'] / track['sampleRate'] * 1000
        print('PASS S-6 schema, reader expectations and sparse WAV timeline:', case)

    # Historical reader input remains valid; D33 also permits these writer transitions.
    vector = json.loads((FIXTURES / 'restored.reader.json').read_text())
    validate(vector['session'])
    for state in vector['states']:
        assert state_at(vector['session'], state['atMs']) == state
    print('PASS S-6 historical restored reader input and D33 writer transition')


if __name__ == '__main__':
    check_fixtures()
