#!/usr/bin/env python3
# SPDX-License-Identifier: CC0-1.0
"""Check S-3a fixture structure/pins/determinism, never derive expected rows."""
import hashlib
import json
import os
from pathlib import Path
import stat
import struct
import subprocess
import sys
import tempfile

HERE=Path(__file__).resolve().parent
FIELDS={'id','origin','lifecycle','interruptedAtMs','transcript','etaMinutes','error','badge','title','startMs','durationMs','names','quote','canWriteDown','speakers'}


def tree(root):
    entries={}
    for parent,dirs,files in os.walk(root,followlinks=False):
        for name in dirs+files:
            path=Path(parent)/name;info=path.lstat()
            value=[stat.S_IFMT(info.st_mode),stat.S_IMODE(info.st_mode)]
            if stat.S_ISLNK(info.st_mode):
                value.append(os.readlink(path))
            elif stat.S_ISREG(info.st_mode):
                digest=hashlib.sha256()
                with path.open('rb') as stream:
                    while block:=stream.read(1<<20):digest.update(block)
                value.extend([info.st_size,digest.hexdigest()])
            entries[str(path.relative_to(root))]=value
    return entries


def check_onboarding_descriptors():
    # Check the producer against the SPEC table independently of either app.
    packets, partial = [], bytearray()
    with (HERE/'assets/onboarding.opus').open('rb') as stream:
        while len(packets) < 2:
            header = stream.read(27)
            assert len(header)==27 and header[:4]==b'OggS'
            lacing = stream.read(header[26])
            payload = stream.read(sum(lacing));offset = 0
            for length in lacing:
                partial.extend(payload[offset:offset+length]);offset += length
                if length < 255:
                    packets.append(bytes(partial));partial.clear()
    packet = packets[1];assert packet[:8]==b'OpusTags'
    vendor_length, = struct.unpack_from('<I',packet,8)
    offset = 12+vendor_length
    count, = struct.unpack_from('<I',packet,offset);offset += 4
    tags = {}
    for _ in range(count):
        size, = struct.unpack_from('<I',packet,offset);offset += 4
        key, value = packet[offset:offset+size].decode().split('=',1);offset += size
        assert key not in tags,'Duplicate producer tag: '+key
        tags[key] = value
    required = ['CASSINI_FORMAT','CASSINI_PROFILE','CASSINI_PAYLOAD_SCHEMA',
                'CASSINI_TRANSCRIPT_IDS','CASSINI_TRANSCRIPT_DEFAULT',
                'CASSINI_AUDIO_SAMPLE_RATE','CASSINI_AUDIO_CHANNELS','CASSINI_AUDIO_SAMPLE_COUNT',
                'CASSINI_AUDIO_DURATION_MS','CASSINI_AUDIO_MATCH_POLICY','CASSINI_AUDIO_OPUS_SHA256',
                'CASSINI_DECODE_HINT']
    for prefix in ('CASSINI_PAYLOAD_','CASSINI_TX_MAIN_PAYLOAD_'):
        required.extend(prefix+suffix for suffix in ('MIME','ENCODING','CHUNK_COUNT','SHA256','RAW_BYTES','GZIP_BYTES','000'))
    assert all(tags.get(name) for name in required),'Required producer descriptor missing/empty'
    assert tags['CASSINI_PAYLOAD_MIME']=='application/vnd.cassini.portable-meeting+json'
    assert tags['CASSINI_PAYLOAD_SCHEMA']=='https://format.gocassini.com/schema/cassini-portable-meeting-manifest-v1.schema.json'
    assert tags['CASSINI_TX_MAIN_PAYLOAD_MIME']=='application/vnd.cassini.transcript-words+json'


def main():
    check_onboarding_descriptors()
    cases=json.loads((HERE/'cases.json').read_text())['cases']
    names={case['id'] for case in cases}
    assert len(names)==len(cases)
    assert {p.stem for p in (HERE/'expected').glob('*.json')}==names
    assert {f'R{i}' for i in range(1,10)} <= {rule for case in cases for rule in case['rules']}
    for case in cases:
        expected=json.loads((HERE/'expected'/f"{case['id']}.json").read_text())
        assert set(expected)=={'scan','groups'}
        assert expected['scan'] in ('ok','empty','failed')
        for group in expected['groups']:
            assert set(group)=={'title','meetings'}
            for row in group['meetings']:
                assert set(row)==FIELDS,(case['id'],set(row)^FIELDS)
                assert all(set(s)=={'id','name','colourIndex'} for s in row['speakers'])
    with tempfile.TemporaryDirectory(prefix='cassini-library-vectors-') as temporary:
        a,b=Path(temporary)/'a',Path(temporary)/'b'
        for out in (a,b):subprocess.run([sys.executable,str(HERE/'make.py'),str(out)],check=True)
        assert tree(a)==tree(b),'Fixture generation is nondeterministic'
    print(f'Verified {len(cases)} hand-authored cases, R1–R9 coverage and byte-identical builds')


if __name__=='__main__':main()
