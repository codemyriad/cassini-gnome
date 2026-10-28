#!/usr/bin/env python3
"""Native management framing bridge for the private browser fixture broker."""
import json
import os
import socket
import struct
import sys
import threading

incoming = open(sys.stdin.fileno(), "rb", buffering=0, closefd=False)
connection = socket.socket(socket.AF_UNIX)
connection.connect(os.environ["CASSINI_CONTROL_SOCKET"])
stream = connection.makefile("rwb")
done = threading.Event()


def exact(size):
    result = bytearray()
    while len(result) < size:
        value = incoming.read(size - len(result))
        if not value:
            return None
        result.extend(value)
    return bytes(result)


def events():
    try:
        while (prefix := exact(4)) is not None:
            payload = exact(struct.unpack("=I", prefix)[0])
            if payload is None:
                break
            packet = json.loads(payload)
            stream.write(json.dumps(packet).encode() + b"\n")
            stream.flush()
    finally:
        done.set()
        connection.shutdown(socket.SHUT_RDWR)


def commands():
    try:
        while (line := stream.readline()):
            payload = json.dumps(json.loads(line)).encode()
            sys.stdout.buffer.write(struct.pack("=I", len(payload)) + payload)
            sys.stdout.buffer.flush()
    finally:
        done.set()
        connection.shutdown(socket.SHUT_RDWR)


threads = [threading.Thread(target=pump, daemon=True) for pump in (events, commands)]
for thread in threads:
    thread.start()
done.wait()
for thread in threads:
    thread.join(timeout=1)
