#!/usr/bin/env python3
"""Test-only transparent bridge. Never starts a recorder or uses the production socket."""
import json
import os
import selectors
import socket
import struct
import sys

with socket.socket(socket.AF_UNIX) as peer:
    peer.connect(os.environ['CASSINI_CONTROL_SOCKET'])
    native = bytearray()
    broker = bytearray()
    with selectors.DefaultSelector() as poll:
        poll.register(sys.stdin, selectors.EVENT_READ, 'native')
        poll.register(peer, selectors.EVENT_READ, 'broker')
        while True:
            for key, _ in poll.select():
                data = os.read(key.fileobj.fileno(), 65536)
                if not data:
                    sys.exit(0)
                if key.data == 'native':
                    native.extend(data)
                    while len(native) >= 4:
                        size, = struct.unpack('=I', native[:4])
                        if len(native) < size + 4:
                            break
                        message = json.loads(native[4:size+4])
                        del native[:size+4]
                        if os.environ.get('CASSINI_CONTROL_FAULT') == 'status' and message['type'] == 'status':
                            message['recording'] = not message['recording']
                        peer.sendall((json.dumps(message) + '\n').encode())
                else:
                    broker.extend(data)
                    while b'\n' in broker:
                        line, _, rest = broker.partition(b'\n')
                        broker = bytearray(rest)
                        message = json.loads(line)
                        if os.environ.get('CASSINI_CONTROL_FAULT') == 'tab':
                            message['tabId'] = 99
                        payload = json.dumps(message).encode()
                        sys.stdout.buffer.write(struct.pack('=I', len(payload)) + payload)
                        sys.stdout.buffer.flush()
