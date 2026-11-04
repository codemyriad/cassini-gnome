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
CASE_COUNT=20
# The three outcome states and their control (D67), then guards: R3/D67.2 failed shown as stored, R13.1 done
# and every track, R13.3 place.
T8_STATES=['t8-control-others-spoke','t8-others-silent','t8-others-never-captured','t8-others-transcription-failed']
T8=T8_STATES+['t8-failed-referenced-words','t8-retry-failed-stale-silent','t8-silent-per-source']
# The 13 cases before T8, digested by legacy_digest: the additive toneFrames key left their bytes unchanged.
LEGACY={
    'empty':'eab928e1ec07bbfb4f34bdbd051c912df33923043ad84f244e4fc8062d846092',
    'first-record':'eab928e1ec07bbfb4f34bdbd051c912df33923043ad84f244e4fc8062d846092',
    'main':'1b03c9b1bed9761218b38c17d340864eb0d8b6da76073fffe2eef4942b674a6b',
    'r1-discovery':'521eab0b1fe813791406a395b3be6f645943115354d501386cb666589ae8789a',
    'r2-whole-document':'f538d51605cb9789fa79d4b9e9d466881faa3dff8402ac5b8bd1e210c69dfb4f',
    'r3-transcript-precedence':'f8fa462560d11564ff670773e58c75e67a8864b66104264ebbbab2b9ff2e890c',
    'r4-capture-history':'84be9fb6720d99b403c53e3737cfa5f716c988d18b7a63b6ea134116f9067cf2',
    'r5-display-duration':'8a4f719b7c6f3202ccc10d9a2a1f5314d7c8b087b46fc9cdb4af2275c95af2ff',
    'r6-title-start-order':'6d6021f9631c70ddeb1774155054635bd94e24ccf4aeb0efc37ce3a05039060f',
    'r7-speaking-order':'2bc44134e478bc2e99a47d6c08aba5353203458cfbd029659960d6a97b1cc560',
    'r8-consecutive-quote':'f6dc0ef4cbdf7480271ce679752be82179d7a1843fd7228f86d781b38282605e',
    'r9-received-once':'927f241cc48417a9726f791c3c1434ceb41d7a74a710e9acd3ec0f29ccb48a4e',
    'received-only':'d8d71fbb4de1ae0fe6e47ea4ffb80674d9dec8a0e4d3eccd675cd2cef2c0e343',
}


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


def legacy_digest(root):
    # Bytes, file types and link targets only: permission bits of links and of parents
    # created by mkdir(parents=True) follow the platform and umask, not make.py.
    entries={name:[stat.S_IFMT(value[0])]+value[2:] for name,value in tree(root).items()}
    return hashlib.sha256(json.dumps(entries,sort_keys=True).encode()).hexdigest()


def check_outcome_lines(cases):
    """T8 (D67): shape and coverage of the hand-written Details lines; never derives them."""
    lines=json.loads((HERE/'outcome-lines.json').read_text())
    assert set(lines)=={'version','note','cases'} and lines['version']==1
    assert [case['id'] for case in cases if 'T8' in case['rules']]==T8 and list(lines['cases'])==T8
    catalog=HERE.parent/'strings.json'  # Beside the vectors in ux-draft; app copies vendor it elsewhere.
    entries={e['id']:e for e in json.loads(catalog.read_text())['entries']} if catalog.exists() else None
    for case in T8:
        value=lines['cases'][case]
        assert set(value)=={'audio','noSpeech','visibleWords'} and type(value['noSpeech']) is bool
        assert all(type(n) is int and n>0 for n in value['visibleWords'].values())
        for line in value['audio']:
            assert set(line)=={'id','args','problem'} and type(line['problem']) is bool,(case,line)
            assert line['id'].startswith('details.sources.')
            source=line['args'].get('source')
            assert isinstance(source,dict) and len(source)==1 and set(source)<={'catalog','text'},(case,line)
            if 'duration' in line['args']:
                assert set(line['args']['duration'])=={'ms'} and type(line['args']['duration']['ms']) is int
            if entries is not None:
                assert line['id'] in entries and ('catalog' not in source or source['catalog'] in entries),(case,line)
                assert set(line['args'])==set(entries[line['id']].get('placeholders',{})),(case,line)
    # Three distinct states (D67.1): each outcome differs from the control and from each other.
    shown=[json.dumps(lines['cases'][case],sort_keys=True) for case in T8_STATES]
    assert len(set(shown))==len(T8_STATES),'T8 outcome lines must differ pairwise'
    # Others silent is visible in Details itself, not only through missing words.
    assert lines['cases'][T8[1]]['audio']!=lines['cases'][T8[0]]['audio']
    guards({case['id']:case for case in cases},lines['cases'])
    return entries is not None


def guards(cases,lines):
    """The guard cases pin what the four states cannot tell apart (review of T8); never derives lines."""
    def stored(case,name):
        (entry,)=[e for e in cases[case]['entries'] if e['path'].endswith('/'+name)]
        return entry['value']
    def silent_sources(case):
        return [line['args']['source']['text'] for line in lines[case]['audio'] if line['id']=='details.sources.silent']
    # D67.2: a failed meeting that references a readable body shows its words as stored.
    case='t8-failed-referenced-words'
    meeting=stored(case,'meeting.json')
    assert meeting['transcript']['status']=='failed' and meeting['transcript']['reference'] is not None
    assert lines[case]['visibleWords'] and 'R3' in cases[case]['rules']
    # R13.1 transcript done: a stale writingDown listing every track, transcript failed, no silent line.
    case='t8-retry-failed-stale-silent'
    meeting,session=stored(case,'meeting.json'),stored(case,'session.json')
    assert meeting['transcript']['status']!='done'
    assert set(meeting['recordingDetails']['writingDown']['silentTracks'])=={t['id'] for t in session['tracks']}
    assert not silent_sources(case)
    # R13.1 every track and R13.3 place: two two-track sources, one all silent, one partly; the silent line is
    # not the last line, so it sits right after its own source's line.
    case='t8-silent-per-source'
    meeting,session=stored(case,'meeting.json'),stored(case,'session.json')
    listed=set(meeting['recordingDetails']['writingDown']['silentTracks'])
    rows=[r for r in session['captureOutcome']['sources'] if len(r['trackIds'])>1]
    assert len(rows)==2 and sum(set(r['trackIds'])<=listed for r in rows)==1
    assert sum(bool(set(r['trackIds'])&listed) and not set(r['trackIds'])<=listed for r in rows)==1
    audio=lines[case]['audio']
    (at,)=[i for i,line in enumerate(audio) if line['id']=='details.sources.silent']
    assert 0<at<len(audio)-1 and audio[at-1]['args']['source']==audio[at]['args']['source']


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
    assert len(cases)==CASE_COUNT,len(cases)
    assert {f'R{i}' for i in range(1,10)}|{'T8'} <= {rule for case in cases for rule in case['rules']}
    for case in cases:
        for entry in case['entries']:
            if 'toneFrames' in entry:
                assert entry['type']=='wav' and entry.get('tone') is True and 'T8' in case['rules'],case['id']
                assert type(entry['toneFrames']) is int and 0<=entry['toneFrames']<=entry['frames'],case['id']
    catalog_checked=check_outcome_lines(cases)
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
        assert {name:legacy_digest(a/name) for name in LEGACY}==LEGACY,'A pre-T8 case changed bytes'
    print(f'Verified {len(cases)} hand-authored cases, R1–R9 and T8 coverage, byte-identical builds, '
          f'{len(LEGACY)} unchanged pre-T8 trees and T8 outcome lines'
          +(' against strings.json' if catalog_checked else ' (catalog ids checked where strings.json is beside)'))


if __name__=='__main__':main()
