#!/usr/bin/env python3
"""Request-derived output conformance. Failed sessions are retained for inspection.

Every case runs its host with a fresh private Library: CASSINI_LIBRARY_DIR (the
test-only override every host honours) plus HOME/XDG directories under the same
temporary folder. A host that writes a meeting anywhere else stops the whole run.
"""
import base64, datetime, json, math, os, pathlib, selectors, shutil, struct, subprocess, sys, tempfile, threading, time, uuid
from jsonschema import Draft202012Validator, FormatChecker
ROOT = pathlib.Path(__file__).resolve().parent.parent
VALIDATORS = {name: Draft202012Validator(json.loads((ROOT/'schemas'/(name+'.schema.json')).read_text()), format_checker=FormatChecker()) for name in ['response','session']}

def read_exact(stream, size):
    result = b''; deadline = time.monotonic() + 15
    with selectors.DefaultSelector() as selector:
        selector.register(stream, selectors.EVENT_READ)
        while len(result) < size:
            assert selector.select(max(0, deadline-time.monotonic())), 'host response timed out'
            chunk = os.read(stream.fileno(), size-len(result))
            assert chunk, 'unexpected host EOF'
            result += chunk
    return result

def response(stream):
    size, = struct.unpack('=I', read_exact(stream,4)); assert 0 < size <= 524288, 'invalid response length'
    value = json.loads(read_exact(stream,size))
    VALIDATORS['response'].validate(value)
    return value

def frame(raw, override=None):
    if override:
        data = bytes.fromhex(override.get('payloadHex',''))
        return struct.pack('=I', override['length']) + data
    data = raw.encode(); return struct.pack('=I',len(data)) + data

def rounded(value):
    return math.floor(value+0.5) if value >= 0 else math.ceil(value-0.5)

def clean_label(value):
    import unicodedata
    return ''.join(c for c in value if unicodedata.category(c) not in ['Cc','Cf'])[:200]

class LibraryEscape(AssertionError):
    """A host ignored the test Library and wrote somewhere real."""

def isolated(temporary):
    temporary = pathlib.Path(temporary)
    library, home = temporary/'Library', temporary/'home'
    library.mkdir(mode=0o700); home.mkdir(mode=0o700)
    env = dict(os.environ, CASSINI_LIBRARY_DIR=str(library), HOME=str(home),
               XDG_CONFIG_HOME=str(home/'.config'), XDG_DATA_HOME=str(home/'.local/share'),
               XDG_MUSIC_DIR=str(home/'Music'))
    return library, env

def check_session(directory, case, accepted, stopped, stop_response, start_request):
    manifest = json.loads((directory/'session.json').read_text())
    VALIDATORS['session'].validate(manifest)
    assert uuid.UUID(manifest['id']) == uuid.UUID(directory.name[len('Recording-'):]), 'session ID differs from folder UUID'
    assert manifest['source'] == 'chromium', 'browser host wrote wrong source'
    if start_request.get('title') is not None:
        assert manifest['title'] == start_request['title'][:500], 'title differs from request'
    assert len(manifest['tracks']) == len(accepted), 'wrong number of accepted tracks'
    by_id = {track['id']:track for track in manifest['tracks']}
    assert set(by_id) == set(accepted), 'wrong or duplicate track IDs'
    for id, expected in accepted.items():
        track = by_id[id]
        for key in ['kind','sampleRate','channels','label']: assert track[key] == expected[key], (id,key,track[key],expected[key])
        assert 0 < len(id.encode()) <= 200, 'ID exceeds UTF-8 byte bound'
        start = expected['start']
        assert track.get('startOffsetMs') == start, 'first accepted offset was not preserved'
        count = len(expected['pcm'])//(track['channels']*2)
        assert track['frames'] == count, 'manifest frames differ from accepted samples plus gaps'
        if track.get('endOffsetMs') is not None:
            assert start is not None and abs(track['endOffsetMs']-(start+count/track['sampleRate']*1000)) < 1e-6, 'wrong source end offset'
        if 'ended' in track: assert track['ended'] is True, 'source not finalized'
        if 'labelSource' in track: assert track['labelSource'] == expected['labelSource'], 'label provenance differs'
        assert pathlib.Path(track['file']).name == track['file'] and track['file'].startswith('track-'), 'unsafe WAV basename'
        path = (directory/track['file']).resolve()
        assert path.parent == directory.resolve(), 'WAV escapes session folder'
        wav = path.read_bytes()
        assert len(wav)>=44 and wav[:4]==b'RIFF' and wav[8:16]==b'WAVEfmt ' and wav[36:40]==b'data', 'invalid PCM WAV header'
        assert struct.unpack_from('<I',wav,4)[0]+8==len(wav) and struct.unpack_from('<I',wav,16)[0]==16
        assert struct.unpack_from('<HHIIHH',wav,20)==(1,expected['channels'],expected['sampleRate'],expected['sampleRate']*expected['channels']*2,expected['channels']*2,16), 'WAV fmt differs from request'
        assert struct.unpack_from('<I',wav,40)[0]==len(wav)-44==len(expected['pcm']), 'WAV data length differs from requests'
        assert wav[44:] == expected['pcm'], 'WAV samples/gap silence differ from accepted requests'
    empty = all(not track['pcm'] for track in accepted.values())
    problem = stopped.get('problem') if stopped else None
    state = 'interrupted' if stopped is None or empty or problem else 'completed'
    assert manifest['state'] == case.get('state',state), 'incorrect final lifecycle state'
    if problem:
        assert manifest['problem']==problem and stop_response.get('problem')==problem, 'stop problem was not echoed'
    if manifest['state']=='completed': assert manifest.get('problem') is None
    elif stop_response: assert stop_response.get('problem')==manifest['problem'], 'response and manifest problem differ'
    if 'frames' in case: assert [track['frames'] for track in manifest['tracks']]==case['frames']
    if 'labelSources' in case: assert [track.get('labelSource') for track in manifest['tracks']]==case['labelSources']
    if 'tracks' in case:
        for id, fields in case['tracks'].items():
            for key,value in fields.items(): assert by_id[id].get(key)==value,(id,key)
    return manifest

def run(host, case, parent=None):
    temporary = tempfile.mkdtemp(prefix='cassini-conformance-', dir=parent)
    library, env = isolated(temporary)
    process = subprocess.Popen([host],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=env)
    stderr = bytearray()
    def drain():
        for chunk in iter(lambda:os.read(process.stderr.fileno(),8192),b''):
            stderr.extend(chunk)
            if len(stderr)>16384: del stderr[:-16384]
    reader=threading.Thread(target=drain,daemon=True);reader.start()
    directory=None;accepted={};stopped=None;stop_response=None;start_request={};failed_response=False
    try:
        assert len(case['requests'])==len(case['expect'])
        wires=[frame(raw,case.get('framing')) for raw in case['requests']]
        if case.get('pipelined'):
            process.stdin.write(b''.join(wires));process.stdin.flush()
        if case.get('eofBeforeResponse'):
            if not case.get('pipelined'): process.stdin.write(b''.join(wires));process.stdin.flush()
            process.stdin.close()
        for index,(raw,expected) in enumerate(zip(case['requests'],case['expect'])):
            if not case.get('pipelined') and not case.get('eofBeforeResponse'):
                wire=wires[index]
                for part in [wire[:2],wire[2:7],wire[7:]]: process.stdin.write(part);process.stdin.flush()
            result=response(process.stdout)
            for key,value in expected.items(): assert result.get(key)==value,(case['name'],expected,result)
            try: request=json.loads(raw)
            except (ValueError,TypeError): request={}
            if result['type']=='started':
                directory=pathlib.Path(result['path'])
                assert directory.is_absolute() and directory.name.startswith('Recording-'), 'invalid started path'
                if not directory.resolve().is_relative_to(library.resolve()):
                    raise LibraryEscape(f'host ignored CASSINI_LIBRARY_DIR and wrote {directory}; left in place, remove it by hand')
                uuid.UUID(directory.name[len('Recording-'):]);assert directory.is_dir()
                start_request=request
            elif result['type']=='ack' and request['type']=='track':
                accepted[request['id']]=dict(kind=request['kind'],sampleRate=request['sampleRate'],channels=request['channels'],label=clean_label(request.get('label') if request.get('label') is not None else request['kind']),labelSource=request.get('labelSource') or 'browser',start=None,pcm=b'')
            elif result['type']=='ack' and request['type']=='audio':
                track=accepted[request['id']];rate=track['sampleRate'];channels=track['channels'];offset=request['offsetMs']
                if track['start'] is None: track['start']=offset
                target=rounded((offset-track['start'])*rate/1000);written=len(track['pcm'])//(channels*2);delta=target-written
                if delta>rate//50: track['pcm']+=b'\0'*(delta*channels*2)
                track['pcm']+=base64.b64decode(request['pcm'],validate=True)
            elif result['type']=='stopped':
                assert directory and pathlib.Path(result['path'])==directory, 'stopped path differs from started path'
                stopped=request;stop_response=result
            elif result['type']=='error':
                failed_response=True
                process.wait(timeout=5) # Must exit without needing EOF from the runner.
                assert process.returncode!=0, 'host returned success after protocol error'
            if case.get('fault')==dict(afterResponse=index,kind='block-checkpoint'):
                assert directory
                (directory/'session.json').unlink();(directory/'session.json').mkdir()
        if not process.stdin.closed: process.stdin.close()
        process.wait(timeout=5)
        assert (process.returncode!=0 if failed_response else process.returncode==0), 'unexpected host exit status'
        assert process.stdout.read()==b'', 'unsolicited response after final request'
        if directory:
            if case.get('fault'):
                assert failed_response and (directory/'session.json').is_dir(), 'IO fault was not exercised'
            else: check_session(directory,case,accepted,stopped,stop_response,start_request)
        shutil.rmtree(temporary)
        print('PASS',case['name']);return True
    except LibraryEscape as error:
        shutil.rmtree(temporary,ignore_errors=True)
        print('FAIL',case['name'],str(error),file=sys.stderr);raise
    except Exception as error:
        if directory is None: shutil.rmtree(temporary,ignore_errors=True)  # nothing to inspect
        print('FAIL',case['name'],str(error),'retained session:',directory,file=sys.stderr);return False
    finally:
        if process.poll() is None:process.kill()
        process.wait(timeout=5);reader.join(timeout=1)
        if stderr:print(stderr.decode('utf-8',errors='replace'),file=sys.stderr)
        for handle in [process.stdin,process.stdout,process.stderr]:
            try: handle.close()
            except OSError: pass

if __name__=='__main__':
    host=str(pathlib.Path(sys.argv[1]).resolve())
    cases=json.loads(pathlib.Path(__file__).with_name('vectors.json').read_text())
    try: results=[run(host,case) for case in cases]
    except LibraryEscape: sys.exit('Stopped: the host wrote outside its test Library; no further cases were run.')
    failed=results.count(False)
    print(f'{len(cases)-failed} passed, {failed} failed');sys.exit(1 if failed else 0)
