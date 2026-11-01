#!/usr/bin/env python3
"""Materialize D52's pinned public-speech corpus, no devices/network/wall waits.

16 kHz originals are repeated three times per sample, attenuated by 1/2 and
placed at 48 kHz. This is a fixture carrier, not a recognition resampler.
Gap ranges follow D52.2 (P7); presenceWindows are generator hints only.
"""
import argparse
from array import array
import hashlib
import json
from pathlib import Path
import sys
import wave

ROOT=Path(__file__).resolve().parent

def load():
    corpus=json.loads((ROOT/'corpus.json').read_text())
    speech={}
    for key,item in corpus['speech'].items():
        path=ROOT/item['file'];assert hashlib.sha256(path.read_bytes()).hexdigest()==item['sha256']
        with wave.open(str(path)) as reader:
            assert (reader.getframerate(),reader.getnchannels(),reader.getsampwidth())==(16000,1,2)
            samples=array('h',reader.readframes(reader.getnframes()))
            if sys.byteorder!='little':samples.byteswap()
            speech[key]=samples
    return corpus,speech

def active(windows, at):return any(a<=at<b for a,b in windows)

def sample(track,at,speech):
    if not active(track.get('presenceWindows',[(track['startMs'],track['endMs'])]),at):return 0
    source=speech[track['speech']]
    position=int((at-track.get('speechStartMs', track['startMs']))*16)
    value=source[position%len(source)]/2 if active(track['speechWindows'],at) else 0
    bleed=track.get('bleed')
    if bleed and active(bleed['windows'],at):
        other=speech[bleed['speech']];bleed_position=int((at-bleed['startMs'])*16)
        value+=other[bleed_position%len(other)]/2*10**(bleed['db']/20)
    return max(-32768,min(32767,round(value)))

def metadata(case):
    tracks=[]
    for track in case['tracks']:
        frames=(track['endMs']-track['startMs'])*48
        value=dict(id=track['id'],kind=track['kind'],label='Guest' if 'sourceTrackId' in track else track['kind'],
                   labelSource='browser' if 'sourceTrackId' in track else 'device',sampleRate=48000,channels=1,
                   file='track-'+track['id']+'.wav',frames=frames,startOffsetMs=track['startMs'],endOffsetMs=track['endMs'],ended=True)
        if 'sourceTrackId' in track:value['sourceTrackId']=track['sourceTrackId']
        # D52.2: a new writer always records gaps; [] means none. Prefixes are not gapFrames.
        value['gaps']=track.get('gaps',[])
        value['gapFrames']=sum(g['frames'] for g in value['gaps'] if g['kind']!='prefix')
        tracks.append(value)
    document=dict(version=1,id=case['id'],createdAt=case['createdAt'],title=case['title'],source='chromium',state='completed',tracks=tracks,events=case['events'])
    long=[t for t in tracks if any(g['kind']!='prefix' and g['frames']>=t['sampleRate'] for g in t['gaps'])]
    if long:
        document.update(state='interrupted',problemKind='gap',problem=' '.join(
            f"{t['gapFrames']*1000//t['sampleRate']} ms of {t['kind']} input time was preserved as silence." for t in long))
    return document

def make(destination):
    corpus,speech=load()
    for case in corpus['sessions']:
        root=destination/('Recording-'+case['id']);root.mkdir(parents=True,exist_ok=False)
        document=metadata(case)
        for track,stored in zip(case['tracks'],document['tracks']):
            with wave.open(str(root/stored['file']),'wb') as writer:
                writer.setnchannels(1);writer.setsampwidth(2);writer.setframerate(48000)
                for first in range(0,stored['frames'],4800):
                    block=array('h',(sample(track,track['startMs']+i/48,speech) for i in range(first,min(first+4800,stored['frames']))))
                    if sys.byteorder!='little':block.byteswap()
                    writer.writeframesraw(block.tobytes())
        (root/'session.json').write_text(json.dumps(document,indent=2)+'\n')
        print(root)

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('destination',type=Path)
    make(parser.parse_args().destination)
