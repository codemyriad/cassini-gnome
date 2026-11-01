#!/usr/bin/env python3
"""Small browser-test writer, not a product host or a conformance implementation.

The shared browser lifecycle tests use it so CI does not depend on either app build.
CASSINI_NATIVE_HOST selects a real app executable instead.
"""
import base64
import datetime
import json
import os
import pathlib
import signal
import struct
import sys
import time
import math
import unicodedata
import uuid
import wave

root = pathlib.Path(sys.argv[1])
manifest = None
directory = None
audio = {}
started_at = None
features = set()
capture_id = None
received = {}
seen = {}

def reply(value):
    payload = json.dumps(value).encode()
    sys.stdout.buffer.write(struct.pack('=I', len(payload)) + payload)
    sys.stdout.buffer.flush()

def checkpoint():
    for track in manifest['tracks']:
        pcm = audio[track['id']]
        track['frames'] = len(pcm) // (track['channels'] * 2)
        with wave.open(str(directory / track['file']), 'wb') as writer:
            writer.setnchannels(track['channels'])
            writer.setsampwidth(2)
            writer.setframerate(track['sampleRate'])
            writer.writeframes(pcm)
    if 'source-outcomes-v1' in features:
        manifest['captureOutcome'] = source_summary()
    temporary = directory / 'session.tmp'
    temporary.write_text(json.dumps(manifest))
    temporary.replace(directory / 'session.json')

def refused(code, message, identifier=None, scope="source"):
    reply(dict(type='error', code=code, message=message, scope=scope,
               **({'id': identifier} if identifier is not None else {})))

def outcome(track):
    frames = received.get(track['id'], 0)
    return ('failed' if track.get('problemKind') else 'neverAudio' if not frames else
            'lowAudio' if frames * 1000 < track['sampleRate'] * 1000 else 'captured')

def source_summary():
    result = []
    for key, source in seen.items():
        tracks = [t for t in manifest['tracks'] if (t.get('sourceTrackId') or t['id']) == key]
        milliseconds = sum(received.get(t['id'], 0) * 1000 / t['sampleRate'] for t in tracks)
        failed = source.get('failed') or any(t.get('problemKind') for t in tracks)
        state = 'failed' if failed else 'neverAudio' if not milliseconds else 'lowAudio' if milliseconds < 1000 else 'captured'
        result.append(dict(sourceTrackId=key, kind=source['kind'], label=source.get('label', source['kind']),
                           captureOutcome=state, audioMs=milliseconds, trackIds=[t['id'] for t in tracks]))
    return dict(sources=result)

def observe_sources(sources):
    if not isinstance(sources, list): raise ValueError('Invalid sources')
    proposed = {key: dict(value) for key, value in seen.items()}
    for source in sources:
        if not isinstance(source, dict): raise ValueError('Invalid source')
        key = source.get('sourceTrackId')
        if not isinstance(key, str) or not key or len(key.encode('utf-8')) > 200:
            raise ValueError('Invalid source identity')
        if source.get('kind') not in ('local','remote','media','tab','application'):
            raise ValueError('Invalid source kind')
        if type(source.get('failed', False)) is not bool: raise ValueError('Invalid failure flag')
        label = source.get('label', source['kind'])
        if not isinstance(label, str) or len(label) > 200: raise ValueError('Invalid source label')
        previous = proposed.get(key, {})
        proposed[key] = dict(sourceTrackId=key, kind=source['kind'], label=source.get('label', source['kind'])[:200],
                             failed=source.get('failed', False) or previous.get('failed', False))
    seen.update(proposed)

def finish(problem=None, reason=None):
    if 'source-outcomes-v1' in features:
        manifest['captureOutcome'] = source_summary()
        for track in manifest['tracks']:
            track['receivedFrames'] = received.get(track['id'], 0)
            track['captureOutcome'] = outcome(track)
        if any(source['captureOutcome'] != 'captured' for source in manifest['captureOutcome']['sources']):
            problem = problem or 'Some announced audio was not fully captured.'
            manifest['problemKind'] = 'io' if any(t.get('problemKind') == 'io' for t in manifest['tracks']) else 'capture'
    manifest['state'] = 'interrupted' if problem else 'completed'
    if problem:
        manifest['problem'] = problem
    for track in manifest['tracks']:
        track['ended'] = True
        if track.get('startOffsetMs') is not None:
            track['endOffsetMs'] = track['startOffsetMs'] + len(audio[track['id']]) / (track['sampleRate'] * track['channels'] * 2) * 1000
    events = manifest.setdefault('events', [])
    if not any(event.get('type') == 'end' for event in events):
        at = max([0, (time.monotonic() - started_at) * 1000] +
                 [t.get('endOffsetMs', 0) for t in manifest['tracks']] + [e['atMs'] for e in events])
        events.append(dict(type='end', atMs=math.ceil(at), reason=reason or ('error' if problem else 'user')))
    checkpoint()

def terminate(_signum, _frame):
    # Chromium can terminate a native host before stdin reports EOF. Complete
    # the fixture's durable interruption checkpoint through the same finally.
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    raise SystemExit(0)

signal.signal(signal.SIGTERM, terminate)
try:
    while True:
        prefix = sys.stdin.buffer.read(4)
        if not prefix:
            break
        size, = struct.unpack('=I', prefix)
        request = json.loads(sys.stdin.buffer.read(size))
        kind = request['type']
        if kind in ('track', 'audio', 'trackEnd') and (manifest is None or manifest['state'] != 'recording'):
            refused('state', 'Capture already saved', request.get('id'), scope='command'); continue
        if kind == 'resume':
            # This per-process fixture owns no app-native stand-in registry.
            # Refusing a fresh resume must never manufacture another folder.
            refused('state', 'No active stand-in is available', scope='command'); continue
        if kind == 'start':
            started_at = time.monotonic()
            features = set(request.get('features', [])) & {'source-outcomes-v1'}
            capture_id = request.get('captureId')
            id = str(uuid.uuid4())
            directory = root / ('Recording-' + id)
            directory.mkdir()
            manifest = dict(version=1, id=id, createdAt=datetime.datetime.now(datetime.timezone.utc).isoformat(), title=request.get('title', 'Browser test'), source='chromium', state='recording', tracks=[])
            checkpoint()
            reply(dict(type='started', path=str(directory), **({'features': sorted(features)} if features else {})))
        elif kind == 'sources' and 'source-outcomes-v1' in features:
            if manifest is None or manifest['state'] != 'recording':
                refused('state', 'Capture already saved', scope='command'); continue
            try:
                if not request.get('discoveredSources'): raise ValueError('Empty source update')
                observe_sources(request['discoveredSources'])
            except ValueError as error:
                refused('source', str(error), scope='command'); continue
            checkpoint(); reply(dict(type='ack'))
        elif kind == 'track':
            existing = next((t for t in manifest['tracks'] if t['id'] == request['id']), None)
            if 'source-outcomes-v1' in features:
                if existing:
                    if existing.get('ended'):
                        refused('state', 'Span already ended', request['id']); continue
                    if all(existing.get(k) == request.get(k) for k in ('kind', 'sampleRate', 'channels', 'sourceTrackId')):
                        reply(dict(type='ack')); continue
                    refused('source', 'Conflicting track declaration', request['id']); continue
                if len(manifest['tracks']) >= 64:
                    refused('limit', 'Track limit reached', request['id']); continue
                key = request.get('sourceTrackId')
                if key is not None and (not isinstance(key, str) or not key or len(key.encode('utf-8')) > 200):
                    refused('source', 'Invalid source identity', request['id']); continue
                if key and any(t.get('sourceTrackId') == key and not t.get('ended') for t in manifest['tracks']):
                    refused('source', 'Overlapping live source span', request['id']); continue
            label = ''.join(c for c in request.get('label', kind) if unicodedata.category(c) not in ['Cc', 'Cf'])[:200]
            track = dict(id=request['id'], label=label, kind=request['kind'], sampleRate=request['sampleRate'], channels=request['channels'], frames=0, file='track-' + str(uuid.uuid4()) + '.wav', labelSource='browser')
            if request.get('sourceTrackId') is not None:
                track['sourceTrackId'] = request['sourceTrackId']
            seen.setdefault(track.get('sourceTrackId') or track['id'], dict(kind=track['kind'], label=label))
            received[track['id']] = 0
            manifest['tracks'].append(track)
            audio[track['id']] = bytearray()
            checkpoint()
            reply(dict(type='ack'))
        elif kind == 'audio':
            track = next((t for t in manifest['tracks'] if t['id'] == request['id']), None)
            if 'source-outcomes-v1' in features:
                if track is None or track.get('ended'):
                    refused('source', 'No live span', request['id']); continue
                if (root / ('fail-audio-' + request['id'])).exists():
                    track['problemKind'] = 'io'; checkpoint()
                    refused('io', 'Generated per-source ENOSPC', request['id']); continue
                end = track.get('startOffsetMs', request['offsetMs']) + len(audio[track['id']]) / (track['sampleRate'] * track['channels'] * 2) * 1000
                if request['offsetMs'] - end > 60000:
                    refused('chunk', 'New span required after suspension', request['id']); continue
            if 'source-outcomes-v1' in features and track.get('sourceTrackId'):
                finish_at = request['offsetMs'] + len(base64.b64decode(request['pcm'])) / (track['sampleRate'] * track['channels'] * 2) * 1000
                conflicts = [t for t in manifest['tracks'] if t is not track and t.get('sourceTrackId') == track['sourceTrackId']
                             and t.get('startOffsetMs') is not None and t.get('endOffsetMs') is not None
                             and max(request['offsetMs'], t['startOffsetMs']) < min(finish_at, t['endOffsetMs'])]
                if conflicts:
                    refused('source', 'Overlapping stored source spans', request['id']); continue
            pcm = audio[track['id']]
            track.setdefault('startOffsetMs', request['offsetMs'])
            delta = round((request['offsetMs'] - track['startOffsetMs']) * track['sampleRate'] / 1000) - len(pcm) // (track['channels'] * 2)
            if delta > track['sampleRate'] // 50:
                pcm.extend(b'\0' * (delta * track['channels'] * 2))
            data = base64.b64decode(request['pcm'])
            pcm.extend(data)
            received[track['id']] += len(data) // (track['channels'] * 2)
            checkpoint()
            reply(dict(type='ack'))
        elif kind == 'trackEnd' and 'source-outcomes-v1' in features:
            track = next((t for t in manifest['tracks'] if t['id'] == request['id']), None)
            if track is None or track.get('ended'):
                refused('state', 'No live span', request['id']); continue
            track['ended'] = True
            track['droppedFrames'] = request['droppedFrames']
            if request['reason'] == 'failed': track.setdefault('problemKind', 'capture')
            if track.get('startOffsetMs') is not None:
                track['endOffsetMs'] = track['startOffsetMs'] + len(audio[track['id']]) / (track['sampleRate'] * track['channels'] * 2) * 1000
            checkpoint(); reply(dict(type='ack'))
        elif kind == 'stop':
            if 'source-outcomes-v1' in features:
                if manifest['state'] != 'recording':
                    if request.get('captureId') == capture_id and not request.get('discoveredSources'):
                        reply(dict(type='stopped', path=str(directory))); continue
                    refused('state', 'Capture already saved', scope='command'); continue
                if capture_id is not None and request.get('captureId') != capture_id:
                    refused('state', 'Capture identity does not match', scope='command'); continue
                try: observe_sources(request.get('discoveredSources', []))
                except ValueError as error:
                    refused('source', str(error), scope='command'); continue
            finish(request.get('problem'), request.get('reason') or 'user')
            reply(dict(type='stopped', path=str(directory), **({'problem': request['problem']} if request.get('problem') else {})))
        else:
            raise ValueError('Unsupported test request')
finally:
    if manifest and manifest['state'] == 'recording':
        finish('Browser disconnected. Audio recorded so far was saved.')
