#!/usr/bin/env python3
# SPDX-License-Identifier: CC0-1.0
"""Build deterministic S-3a Library trees. Python 3.11+, stdlib only, no app imports.

Inputs are cases.json and committed assets. This never reads/writes expected/.
OUT must be absent or empty; no existing Library is ever removed or modified.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import wave

HERE = Path(__file__).resolve().parent


class SparseWriter:
    """Make long silent board fixtures sparse while wave counts every PCM frame."""
    def __init__(self, stream):
        self.stream = stream

    def write(self, data):
        if len(data) >= 4096 and not data.strip(b'\0'):
            self.stream.seek(len(data), 1)
            return len(data)
        return self.stream.write(data)

    def __getattr__(self, name):
        return getattr(self.stream, name)


def wav(path, entry):
    rate, channels, frames = entry.get('rate',8000), entry.get('channels',1), entry['frames']
    with path.open('wb') as stream:
        with wave.open(SparseWriter(stream),'wb') as audio:
            audio.setparams((channels,2,rate,0,'NONE','not compressed'))
            tone = min(frames,rate) if entry.get('tone') else 0
            # An integer square wave, avoiding floating point/platform differences.
            if tone:
                audio.writeframesraw(b''.join((5000 if (i*440*2//rate)%2 else -5000).to_bytes(2,'little',signed=True)*channels for i in range(tone)))
            remaining = (frames-tone)*channels*2
            block = bytes(65536)
            while remaining:
                amount = min(remaining,len(block));audio.writeframesraw(block[:amount]);remaining -= amount
        stream.truncate()
    if entry.get('trailingBytes'):
        with path.open('ab') as stream:
            stream.write(bytes.fromhex(entry['trailingBytes']))
    if entry.get('staleHeaderFrames') is not None:
        import struct
        size = entry['staleHeaderFrames']*channels*2
        with path.open('r+b') as stream:
            stream.seek(4);stream.write(struct.pack('<I',36+size))
            stream.seek(40);stream.write(struct.pack('<I',size))


def build(out):
    if out.is_symlink() or out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise ValueError('OUT must be a new or empty directory, never an existing Library')
    out.mkdir(parents=True,exist_ok=True)
    manifest = json.loads((HERE/'assets/SHA256.json').read_text())
    for name, expected in manifest.items():
        data = (HERE/'assets'/name).read_bytes()
        if len(data)!=expected['bytes'] or hashlib.sha256(data).hexdigest()!=expected['sha256']:
            raise ValueError('asset integrity mismatch: '+name)
    vectors = json.loads((HERE/'cases.json').read_text())
    for case in vectors['cases']:
        directory = out/case['id'];directory.mkdir(mode=0o700)
        if case.get('root','directory')=='directory':
            (directory/'root').mkdir(mode=0o700)
        (directory/'activity.json').write_text(json.dumps(case.get('activity',{}),sort_keys=True,indent=2)+'\n')
        for entry in case['entries']:
            name = Path(entry['path'])
            if name.is_absolute() or '..' in name.parts:
                raise ValueError('unsafe fixture output path')
            path = directory/name;path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
            kind = entry['type']
            if kind=='json':
                path.write_text(json.dumps(entry['value'],ensure_ascii=False,sort_keys=True,indent=2)+'\n')
            elif kind=='text':
                path.write_bytes(entry['value'].encode('utf-8'))
            elif kind=='wav':
                wav(path,entry)
            elif kind=='asset':
                shutil.copyfile(HERE/'assets'/entry['name'],path)
            elif kind=='directory':
                path.mkdir(mode=0o700,exist_ok=True)
            elif kind=='symlink':
                target = (path.parent/entry['target']).resolve()
                if not target.is_relative_to(directory.resolve()):
                    raise ValueError('fixture link must stay in its case directory')
                path.symlink_to(entry['target'])
            elif kind=='fifo':
                os.mkfifo(path,0o600)
            else:
                raise ValueError('unknown fixture entry: '+kind)
            if kind not in ('directory','symlink','fifo'):
                path.chmod(0o600)
        # Stable filesystem times; scanners must not use them as meeting dates.
        for parent, dirs, files in os.walk(directory,followlinks=False):
            for name in dirs+files:
                os.utime(Path(parent)/name,ns=(0,0),follow_symlinks=False)
            os.utime(parent,ns=(0,0),follow_symlinks=False)
    return len(vectors['cases'])


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('out',type=Path)
    args=parser.parse_args()
    print(f'Built {build(args.out)} deterministic Library cases in {args.out}')
