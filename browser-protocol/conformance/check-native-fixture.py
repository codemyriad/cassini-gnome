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

# A gap fixture is a completed/saved interrupted capture, not fabricated continuous input.
gap = root.with_name('native-session-gap')
for name, source in [('session.json','macos'),('session-gnome.json','gnome')]:
    m=json.loads((gap/name).read_text())
    assert m['source']==source and m['state']=='interrupted' and m['problemKind']=='gap'
    assert m['tracks'][1]['gapFrames']==8000
    assert m['tracks'][0]['gaps']==[] and m['tracks'][1]['gaps']==[dict(atMs=1000,durationMs=1000,frames=8000,kind='input')]
    with wave.open(str(gap/m['tracks'][1]['file'])) as audio:
        assert audio.getnframes()==24000;audio.setpos(8000);assert not any(audio.readframes(8000))
    print('PASS native gap fixture:',source)
