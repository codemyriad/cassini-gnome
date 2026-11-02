"""Independent P11 heartbeat-v1 oracle (D52.4); apps run the literal vectors on their own code."""
HEARTBEAT_INTERVAL = 10000
CONTROL_LOSS_AFTER = 25000
MAX_SAFE_INTEGER = 2 ** 53 - 1


def pong_for(message):
    """The extension's exact reply to an app message, or None (no reply, no side effect)."""
    if not isinstance(message, dict) or message.get('type') != 'ping':
        return None
    seq = message.get('seq')
    # An integer JSON number, as JavaScript and JSON Schema see it: 3.0 is 3.
    if type(seq) is float and seq.is_integer():
        seq = int(seq)
    if type(seq) is not int or not 0 <= seq <= MAX_SAFE_INTEGER:
        return None
    return {'type': 'pong', 'seq': seq}


class App:
    """The app side of one browser's control link."""

    def __init__(self):
        self.connected = False

    def step(self, row):
        now, frame = row['atMs'], row.get('in')
        if frame == 'hello':
            # A hello opens a fresh connection; nothing carries over.
            self.connected = True
            self.negotiated = 'heartbeat-v1' in (row.get('capabilities') or [])
            self.active = False
            self.seq = 0
        elif frame is not None and self.connected:
            self.last_inbound = now
            if frame == 'status' and self.negotiated and not self.active:
                self.active, self.last_ping = True, now
        if not self.connected or not self.active:
            return []
        if now - self.last_inbound >= CONTROL_LOSS_AFTER:
            self.connected = False
            return ['controlLost']
        if now - self.last_ping >= HEARTBEAT_INTERVAL:
            self.last_ping = now
            self.seq += 1
            return ['ping:%d' % (self.seq - 1)]
        return []
