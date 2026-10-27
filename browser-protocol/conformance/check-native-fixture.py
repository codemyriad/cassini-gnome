#!/usr/bin/env python3
import json, pathlib, wave
root = pathlib.Path(__file__).with_name('native-session')
for name, source in [('session.json', 'macos'), ('session-gnome.json', 'gnome')]:
    manifest = json.loads((root / name).read_text())
    assert manifest['version'] == 1 and manifest['source'] == source and manifest['state'] == 'completed'
    assert [track['kind'] for track in manifest['tracks']] == ['microphone', 'system']
    for track in manifest['tracks']:
        with wave.open(str(root / track['file'])) as audio:
            assert audio.getnchannels() == track['channels']
            assert audio.getframerate() == track['sampleRate']
            assert audio.getnframes() == track['frames'] == 8000
            assert audio.getsampwidth() == 2
        assert track['endOffsetMs'] == track['startOffsetMs'] + track['frames'] / track['sampleRate'] * 1000
    print('PASS native session fixture:', source)
