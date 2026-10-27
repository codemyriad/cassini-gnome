#!/usr/bin/env python3
"""Portable stdio vectors; each case owns a new host and its generated session."""
import json, os, pathlib, selectors, shutil, struct, subprocess, sys, time

def read_exact(stream, size):
    result = b''
    deadline = time.monotonic() + 15
    with selectors.DefaultSelector() as selector:
        selector.register(stream, selectors.EVENT_READ)
        while len(result) < size:
            assert selector.select(max(0, deadline - time.monotonic())), 'host response timed out'
            chunk = os.read(stream.fileno(), size - len(result))
            assert chunk, 'unexpected host EOF'
            result += chunk
    return result

def response(stream):
    size, = struct.unpack('=I', read_exact(stream, 4))
    assert 0 < size <= 524288, f'invalid response length {size}'
    return json.loads(read_exact(stream, size))

def run(host, case):
    process = subprocess.Popen([host], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    directory = None
    try:
        assert len(case['requests']) == len(case['expect'])
        for raw, expected in zip(case['requests'], case['expect']):
            data = raw.encode('utf-8')
            frame = struct.pack('=I', len(data)) + data
            # Deliberately fragmented transport, including the length prefix.
            for part in (frame[:2], frame[2:7], frame[7:]):
                process.stdin.write(part); process.stdin.flush()
            result = response(process.stdout)
            if result.get('type') == 'started':
                directory = pathlib.Path(result['path'])
                assert directory.is_absolute() and directory.name.startswith('Recording-'), 'unsafe test cleanup path'
            for key, value in expected.items():
                assert result.get(key) == value, (case['name'], expected, result)
        process.stdin.close()
        process.wait(timeout=15)
        if directory:
            manifest = json.loads((directory / 'session.json').read_text())
            if 'state' in case: assert manifest['state'] == case['state'], manifest
            if 'frames' in case: assert [track['frames'] for track in manifest['tracks']] == case['frames'], manifest
            for track in manifest['tracks']:
                assert pathlib.Path(track['file']).name == track['file']
                wav = (directory / track['file']).read_bytes()
                assert wav[:4] == b'RIFF' and wav[8:12] == b'WAVE'
                assert struct.unpack_from('<I', wav, 40)[0] == len(wav)-44
                assert track['frames'] == (len(wav)-44)//(track['channels']*2)
            shutil.rmtree(directory)
        print('PASS', case['name'])
    except Exception as error:
        print('FAIL', case['name'], str(error), 'retained session:', directory, file=sys.stderr)
        return False
    finally:
        if process.poll() is None: process.kill()
        process.wait(timeout=5)
        stderr = process.stderr.read().decode('utf-8', errors='replace')
        if stderr: print(stderr[-8192:], file=sys.stderr)
    return True

if __name__ == '__main__':
    host = str(pathlib.Path(sys.argv[1]).resolve())
    cases = json.loads(pathlib.Path(__file__).with_name('vectors.json').read_text())
    results = [run(host, case) for case in cases]
    failed = results.count(False)
    print(f'{len(cases) - failed} passed, {failed} failed')
    sys.exit(1 if failed else 0)
