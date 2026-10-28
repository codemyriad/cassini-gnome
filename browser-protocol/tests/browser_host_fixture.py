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
import unicodedata
import uuid
import wave

root = pathlib.Path(sys.argv[1])
manifest = None
directory = None
audio = {}

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
    temporary = directory / 'session.tmp'
    temporary.write_text(json.dumps(manifest))
    temporary.replace(directory / 'session.json')

def finish(problem=None):
    manifest['state'] = 'interrupted' if problem else 'completed'
    if problem:
        manifest['problem'] = problem
    for track in manifest['tracks']:
        track['ended'] = True
        if track.get('startOffsetMs') is not None:
            track['endOffsetMs'] = track['startOffsetMs'] + len(audio[track['id']]) / (track['sampleRate'] * track['channels'] * 2) * 1000
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
        if kind == 'start':
            id = str(uuid.uuid4())
            directory = root / ('Recording-' + id)
            directory.mkdir()
            manifest = dict(version=1, id=id, createdAt=datetime.datetime.now(datetime.timezone.utc).isoformat(), title=request.get('title', 'Browser test'), source='chromium', state='recording', tracks=[])
            checkpoint()
            reply(dict(type='started', path=str(directory)))
        elif kind == 'track':
            label = ''.join(c for c in request.get('label', kind) if unicodedata.category(c) not in ['Cc', 'Cf'])[:200]
            track = dict(id=request['id'], label=label, kind=request['kind'], sampleRate=request['sampleRate'], channels=request['channels'], frames=0, file='track-' + str(uuid.uuid4()) + '.wav', labelSource='browser')
            manifest['tracks'].append(track)
            audio[track['id']] = bytearray()
            checkpoint()
            reply(dict(type='ack'))
        elif kind == 'audio':
            track = next(t for t in manifest['tracks'] if t['id'] == request['id'])
            pcm = audio[track['id']]
            track.setdefault('startOffsetMs', request['offsetMs'])
            delta = round((request['offsetMs'] - track['startOffsetMs']) * track['sampleRate'] / 1000) - len(pcm) // (track['channels'] * 2)
            if delta > track['sampleRate'] // 50:
                pcm.extend(b'\0' * (delta * track['channels'] * 2))
            pcm.extend(base64.b64decode(request['pcm']))
            checkpoint()
            reply(dict(type='ack'))
        elif kind == 'stop':
            finish(request.get('problem'))
            reply(dict(type='stopped', path=str(directory), **({'problem': request['problem']} if request.get('problem') else {})))
        else:
            raise ValueError('Unsupported test request')
finally:
    if manifest and manifest['state'] == 'recording':
        finish('Browser disconnected. Audio recorded so far was saved.')
