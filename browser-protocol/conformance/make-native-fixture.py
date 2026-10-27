#!/usr/bin/env python3
"""Regenerate deterministic format fixtures; never reads a hardware device."""
import json, math, pathlib, struct, wave
root = pathlib.Path(__file__).with_name('native-session')
root.mkdir(exist_ok=True)
tracks = []
for label, kind, channels, frequency, offset in [('Microphone', 'microphone', 1, 440, 0), ('System output', 'system', 2, 880, 125)]:
    filename = f'track-{kind}.wav'
    with wave.open(str(root / filename), 'wb') as output:
        output.setnchannels(channels); output.setsampwidth(2); output.setframerate(8000)
        output.writeframes(b''.join(struct.pack('<h', round(12000 * math.sin(2 * math.pi * frequency * frame / 8000))) * channels for frame in range(8000)))
    tracks.append(dict(id=kind, label=label, kind=kind, file=filename, sampleRate=8000, channels=channels,
        startOffsetMs=offset, endOffsetMs=offset+1000, frames=8000, ended=True, labelSource='device'))
for source, filename in [('macos', 'session.json'), ('gnome', 'session-gnome.json')]:
    manifest = dict(version=1,id='9037a57c-c501-44e6-8f1a-56f4c9317695',createdAt='2026-10-27T10:00:00Z',
        title='Synthetic native session fixture',source=source,state='completed',tracks=tracks)
    (root / filename).write_text(json.dumps(manifest,indent=2)+'\n')
