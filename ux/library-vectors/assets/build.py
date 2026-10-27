#!/usr/bin/env python3
# SPDX-License-Identifier: CC0-1.0
"""Rebuild the three generated CC0 Opus references, without any app code.

The Opus silence packet f8 ff fe is RFC 6716's conventional 20 ms silence.
One extra packet permits pre-skip and final trimming; all granules are real.
"""
import base64
import gzip
import hashlib
import json
import math
from pathlib import Path
import struct

ROOT = Path(__file__).resolve().parent
TABLE = []
for byte in range(256):
    value = byte << 24
    for _ in range(8):
        value = ((value << 1) ^ (0x04C11DB7 if value & 0x80000000 else 0)) & 0xFFFFFFFF
    TABLE.append(value)


def page(packets, number, granule, flags):
    segments = []
    for packet in packets:
        segments.extend([255] * (len(packet) // 255) + [len(packet) % 255])
    assert len(segments) <= 255
    header = b'OggS' + struct.pack('<BBQIII', 0, flags, granule, 0x43415353, number, 0) + bytes([len(segments)])
    data = bytearray(header + bytes(segments) + b''.join(packets))
    crc = 0
    for byte in data:
        crc = ((crc << 8) ^ TABLE[((crc >> 24) ^ byte) & 255]) & 0xFFFFFFFF
    struct.pack_into('<I', data, 22, crc)
    return data


def chunk(prefix, body):
    raw = json.dumps(body, ensure_ascii=False, separators=(',', ':')).encode()
    compressed = gzip.compress(raw, mtime=0)
    compressed = compressed[:9] + b"\xff" + compressed[10:]
    encoded = base64.urlsafe_b64encode(compressed).rstrip(b'=').decode()
    return {prefix+'ENCODING':'base64url+gzip+utf8json',prefix+'CHUNK_COUNT':'1',prefix+'SHA256':hashlib.sha256(raw).hexdigest(),prefix+'RAW_BYTES':str(len(raw)),prefix+'GZIP_BYTES':str(len(compressed)),prefix+'000':encoded}


def build(name, duration_ms, *, title=None, portable=False):
    samples = duration_ms * 48
    count = math.ceil((samples + 312) / 960)
    head = b'OpusHead' + struct.pack('<BBHIhB', 1, 1, 312, 48000, 0, 0)
    silence = bytes.fromhex('f8fffe')
    digest = hashlib.sha256(b'org.cassini.opus-packets/1\0')
    canonical_head = bytearray(head);canonical_head[12:16] = bytes(4)
    digest.update(b'H'+struct.pack('<Q',len(head))+canonical_head)
    for _ in range(count):
        digest.update(b'A'+struct.pack('<Q',len(silence))+silence)
    digest.update(b'E'+struct.pack('<QQ',count,samples))
    tags = {} if title is None else {'TITLE':title}
    if portable:
        words = {'format':'cassini.words.v1','language':'en','items':[
            {'speaker':'silvio','startMs':2000,'endMs':8000,'text':'This is where every meeting you record ends up. Click any word and it plays from there.'},
            {'speaker':'guest','startMs':9000,'endMs':13000,'text':'And I do not need to set anything up to record my own?'}]}
        word_tags = chunk('CASSINI_TX_MAIN_PAYLOAD_', words)
        ref = {'prefix':'CASSINI_TX_MAIN_PAYLOAD_','chunkCount':1,'sha256':word_tags['CASSINI_TX_MAIN_PAYLOAD_SHA256'],'rawBytes':int(word_tags['CASSINI_TX_MAIN_PAYLOAD_RAW_BYTES']),'gzipBytes':int(word_tags['CASSINI_TX_MAIN_PAYLOAD_GZIP_BYTES']),'mime':'application/vnd.cassini.transcript-words+json','encoding':'base64url+gzip+utf8json'}
        manifest = {'kind':'cassini-portable-meeting','version':1,'profile':'ogg-opus',
            'meeting':{'id':'mtg_'+digest.hexdigest(),'title':title,'createdAtUtc':'2026-10-23T13:00:00Z','durationMs':duration_ms},
            'audio':{'container':'ogg','codec':'opus','sampleRate':48000,'channels':1,'sampleCount':samples,'durationMs':duration_ms},
            'integrity':{'matchPolicy':'exact-opus-audio-v1','opusAudioSha256':digest.hexdigest(),'sampleRate':48000,'channels':1,'sampleCount':samples,'durationMs':duration_ms},
            'speakers':[{'id':'silvio','label':'Silvio'},{'id':'guest','label':'Speaker 2'}],
            'transcripts':[{'id':'main','role':'raw-asr','default':True,'format':'cassini.words.v1','wordCount':2,'payloadRef':ref}]}
        tags.update({'CASSINI_FORMAT':'org.cassini.portable-meeting/1',**chunk('CASSINI_PAYLOAD_',manifest),**word_tags})
    comments = [(key+'='+value).encode() for key,value in tags.items()]
    vendor = b'Cassini S-3a CC0 reference'
    opus_tags = b'OpusTags'+struct.pack('<I',len(vendor))+vendor+struct.pack('<I',len(comments))+b''.join(struct.pack('<I',len(c))+c for c in comments)
    data = page([head],0,0,2)+page([opus_tags],1,0,0)
    for start in range(0,count,255):
        amount = min(255,count-start);last = start+amount==count
        data += page([silence]*amount,2+start//255,samples+312 if last else (start+amount)*960,4 if last else 0)
    (ROOT/name).write_bytes(data)


if __name__ == '__main__':
    build('plain-untitled.opus',60)
    build('plain-title.opus',60,title='  Vorbis title  ')
    build('onboarding.opus',2040000,title='Onboarding walkthrough',portable=True)
