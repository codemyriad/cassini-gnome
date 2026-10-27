#!/usr/bin/env python3
"""Headless control-host bridge conformance, with isolated test-only broker IPC.

HOST may be a launcher adapting private product IPC to the NDJSON test socket.
No browser, UI, device or audio recorder is started.
"""
import json
import os
from pathlib import Path
import selectors
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[2]
VALIDATOR = Draft202012Validator(json.loads((ROOT / 'schemas/control-extension.schema.json').read_text()))


def exact(stream, count):
    result = b''
    deadline = time.monotonic() + 3
    with selectors.DefaultSelector() as poll:
        poll.register(stream, selectors.EVENT_READ)
        while len(result) < count:
            assert poll.select(max(0, deadline - time.monotonic())), 'native host response timed out'
            data = os.read(stream.fileno(), count - len(result))
            assert data, 'unexpected native host EOF'
            result += data
    return result


def native_read(stream):
    length, = struct.unpack('=I', exact(stream, 4))
    assert 0 < length <= 524288, 'invalid native frame size'
    return json.loads(exact(stream, length))


def native_write(stream, value):
    VALIDATOR.validate(value)
    data = json.dumps(value).encode()
    stream.write(struct.pack('=I', len(data)) + data)
    stream.flush()


def status(recording=False, tab_id=None):
    return dict(type='status', recording=recording, tabId=tab_id,
                path='/test/Recording-example' if recording else None,
                message='Recording' if recording else 'Ready', tracks=1 if recording else 0, stopping=False)


def connection(host, listener, env, case, resumed=False):
    process = subprocess.Popen([str(host)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    errors = bytearray()
    def drain():
        for data in iter(lambda: process.stderr.read(4096), b''):
            errors.extend(data)
            if len(errors) > 16384:
                del errors[:-16384]
    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    peer = None
    try:
        hello = dict(type='hello', version=1)
        native_write(process.stdin, hello)
        peer, _ = listener.accept()
        peer.settimeout(3)
        broker = peer.makefile('r', encoding='utf-8')
        def received(expected):
            line = broker.readline(524289)
            assert line and len(line.encode()) <= 524288, 'missing/oversize broker envelope'
            actual = json.loads(line)
            assert actual == expected, ('broker envelope differs', expected, actual)
        received(hello)
        count = case.get('sourceCount', 3)
        snapshot = dict(type='meetings', meetings=[dict(tabId=2, title='Meeting B', url='https://meet.example/call', sourceCount=count)] if count else [])
        for packet in [snapshot, status(resumed, 2 if resumed else None)]:
            native_write(process.stdin, packet)
            received(packet)
        if not resumed:
            for command, expected in zip(case.get('commands', []), case.get('expect', [])):
                peer.sendall((json.dumps(command) + '\n').encode())
                assert native_read(process.stdout) == command, 'host changed/dropped command or tabId'
                result = dict(type='result', requestId=command['requestId'], ok=expected['ok'])
                if expected['ok']:
                    result['status'] = status(expected['recording'], expected.get('tabId'))
                else:
                    result['error'] = expected['error']
                native_write(process.stdin, result)
                received(result)
        process.stdin.close()
        process.wait(timeout=3)
        assert process.returncode == 0, 'host failed on ordinary control EOF'
        assert process.stdout.read() == b'', 'host fabricated commands after EOF'
        broker.close()
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=3)
        if peer:
            peer.close()
        reader.join(timeout=1)
        for handle in [process.stdin, process.stdout, process.stderr]:
            handle.close()


def run(host, case):
    try:
        with tempfile.TemporaryDirectory(prefix='cassini-control-') as temporary:
            directory = Path(temporary)
            directory.chmod(0o700)
            endpoint = directory / 'broker.sock'
            with socket.socket(socket.AF_UNIX) as listener:
                listener.bind(str(endpoint))
                endpoint.chmod(0o600)
                listener.listen()
                listener.settimeout(3)
                env = dict(os.environ, CASSINI_CONTROL_SOCKET=str(endpoint))
                connection(host, listener, env, case)
                if case.get('kind') == 'reconnect':
                    connection(host, listener, env, case, resumed=True)
        print('PASS', case['name'])
        return True
    except Exception as error:
        print('FAIL', case['name'], str(error), file=sys.stderr)
        return False


if __name__ == '__main__':
    host = Path(sys.argv[1]).resolve()
    cases = json.loads(Path(__file__).with_name('vectors.json').read_text())
    results = [run(host, case) for case in cases]
    print(f'{sum(results)} passed, {results.count(False)} failed')
    sys.exit(0 if all(results) else 1)
