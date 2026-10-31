# SPDX-License-Identifier: GPL-3.0-or-later
"""D36 private meter role: bounded columns, never PCM or extension messages."""

import base64
import errno
import json
import math
import select
import socket
from pathlib import Path

LIMIT = 32768


def serve_meter(service, connection, stream, directory):
    from .browser_service import send_packet

    with service.lock:
        capture = next(
            (
                c
                for c in service.captures.values()
                if c.directory is not None and str(c.directory) == directory and not c.finished
            ),
            None,
        )
    if capture is None:
        send_packet(stream, dict(type="error", message="This meeting is no longer recording"))
        return
    source = capture.spectrogram
    with capture.lock:
        capture._sync_spectrogram()
        subscription = source.subscribe(capture.elapsed_ms())
    connection.settimeout(0.5)
    try:
        while not service.closed.is_set():
            now = capture.elapsed_ms()
            lanes = source.drain(subscription, now)
            send_packet(
                stream,
                dict(
                    type="columns",
                    nowMs=now,
                    finished=source.finished,
                    lanes={
                        side: [[slot, flags, base64.b64encode(bands).decode("ascii")] for slot, flags, bands in rows]
                        for side, rows in lanes.items()
                    },
                ),
            )
            if source.finished:
                break
            readable, _, _ = select.select([connection], [], [], 0.04)
            if readable:
                # The role is one-way after subscribing. EOF releases its
                # producer immediately, rather than waiting for the next send.
                connection.recv(1)
                break
    finally:
        source.unsubscribe(subscription)


def decode_columns(packet):
    if not isinstance(packet, dict) or packet.get("type") != "columns":
        raise ValueError("Invalid meter packet")
    now = packet.get("nowMs")
    if type(now) not in (int, float) or not math.isfinite(now) or not 0 <= now <= 2**53:
        raise ValueError("Invalid meter clock")
    lanes = packet.get("lanes")
    if not isinstance(lanes, dict) or set(lanes) != {"me", "others"}:
        raise ValueError("Invalid meter lanes")
    decoded = {}
    for side, rows in lanes.items():
        if not isinstance(rows, list) or len(rows) > 64:
            raise ValueError("Unbounded meter columns")
        result = []
        for row in rows:
            if not isinstance(row, list) or len(row) != 3:
                raise ValueError("Invalid meter column")
            slot, flags, encoded = row
            if type(slot) is not int or not 0 <= slot < 2**32 or type(flags) is not int or not 0 <= flags <= 3:
                raise ValueError("Invalid meter slot/flags")
            if not isinstance(encoded, str) or len(encoded) != 44:
                raise ValueError("Invalid meter bands")
            bands = base64.b64decode(encoded, validate=True)
            if len(bands) != 32:
                raise ValueError("Invalid meter band count")
            result.append((slot, flags, bands))
        decoded[side] = result
    return now, decoded, packet.get("finished") is True


class MeterClient:
    """Nonblocking GTK reader; split/combined JSON lines never block a frame."""

    def __init__(self, path, directory, columns, closed=None):
        from gi.repository import GLib

        self.glib = GLib
        self.columns, self.closed = columns, closed
        self.buffer = bytearray()
        self.watch = 0
        self.socket = socket.socket(socket.AF_UNIX)
        self.socket.setblocking(False)
        self.request = bytearray((json.dumps(dict(role="meter", path=str(directory))) + "\n").encode())
        self.problem = None
        self.finished = False
        self._closed = False
        code = self.socket.connect_ex(str(Path(path)))
        if code not in (0, errno.EINPROGRESS, errno.EAGAIN, errno.EWOULDBLOCK):
            self.socket.close()
            self._closed = True
            raise OSError(code, "Could not connect recording meter")
        self.watch = GLib.io_add_watch(
            self.socket, GLib.PRIORITY_DEFAULT, GLib.IO_OUT | GLib.IO_HUP | GLib.IO_ERR, self._connected
        )

    def _connected(self, _socket, condition):
        GLib = self.glib
        try:
            error = self.socket.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
            if error or condition & (GLib.IO_HUP | GLib.IO_ERR):
                raise OSError(error or errno.ECONNRESET, "Recording meter connection failed")
            try:
                count = self.socket.send(self.request)
            except BlockingIOError:
                return True
            del self.request[:count]
            if self.request:
                return True
            self.watch = GLib.io_add_watch(
                self.socket, GLib.PRIORITY_DEFAULT, GLib.IO_IN | GLib.IO_HUP | GLib.IO_ERR, self._read
            )
        except (OSError, ValueError) as error:
            self._failed(error)
        return False

    def _read(self, _socket, condition):
        try:
            try:
                data = self.socket.recv(LIMIT)
            except BlockingIOError:
                return True
            if not data:
                self.close(notify=True)
                return False
            self.buffer.extend(data)
            if len(self.buffer) > LIMIT:
                raise ValueError("Unbounded recording meter input")
            while b"\n" in self.buffer:
                line, _, rest = self.buffer.partition(b"\n")
                self.buffer = bytearray(rest)
                now, lanes, finished = decode_columns(json.loads(line))
                self.columns(now, lanes)
                if finished:
                    self.finished = True
                    self.close(notify=True)
                    return False
            if condition & self.glib.IO_ERR:
                raise OSError("Recording meter read failed")
        except Exception as error:
            self._failed(error)
            return False
        return True

    def _failed(self, error):
        self.problem = str(error)
        self.close(notify=True)

    def close(self, *, notify=False):
        if self._closed:
            return
        self._closed = True
        if self.watch:
            if self.glib.MainContext.default().find_source_by_id(self.watch):
                self.glib.source_remove(self.watch)
            self.watch = 0
        self.socket.close()
        self.buffer.clear()
        if notify and self.closed:
            self.closed(self)
