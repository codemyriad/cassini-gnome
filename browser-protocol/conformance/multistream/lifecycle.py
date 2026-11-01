"""Independent D52 oracle; desktop tests run the literal vectors on their own policy."""
class Policy:
    def __init__(self, source='browser', reason='start'):
        self.source, self.reason = source, reason
        self.heard = self.stopped = False
        self.ended = self.closed = self.missing = None

    def step(self, row):
        if self.stopped: return []
        now, page = row['atMs'], row.get('pageState', 'present')
        count = row.get('sourceCount')
        known = row.get('rowPresent', True) and row.get('sourceCountKnown', count is not None)
        self.heard |= row.get('heard') is True
        if page == 'closed':
            self.closed = now if self.closed is None else self.closed
        if self.closed is not None:
            if now - self.closed < 2000: return []
            self.stopped = True; return ['stop:tabClosed']
        transport = row.get('transportAlive', True)
        if not row.get('controlAlive', True) and row.get('pcmLive') and transport:
            self.missing = self.ended = None; return []
        if self.missing is not None and now - self.missing >= 120000:
            self.stopped = True; return ['stop:pageGone']
        actions = []
        if not transport and self.source != 'system':
            # D52.4: a closed audio transport (EOF, error or PCM_STALE_AFTER)
            # falls back at once. It is not missing-page evidence by itself.
            self.source, self.reason = 'system', 'lost'; actions = ['fallback']
        if page == 'present' and row.get('controlAlive', True) and known and count > 0:
            # D52.4 Q3: a visibly ongoing call stays on fallback without a
            # pageGone deadline; restoring needs a live audio transport.
            self.missing = self.ended = None
            if transport and self.source == 'system' and self.heard:
                action = 'switch:browser:callConnected' if self.reason == 'missing' else 'restore'
                self.source, self.reason = 'browser', 'restored'; return [action]
            return actions
        if page != 'present' or not row.get('controlAlive', True) or self.missing is not None:
            self.ended = None
            self.missing = now if self.missing is None else self.missing
            if now - self.missing >= 2000 and self.source != 'system':
                self.source, self.reason = 'system', 'lost'; return ['fallback']
            return actions
        if not known:
            self.ended = None; return actions
        if self.heard:
            self.ended = now if self.ended is None else self.ended
            if now - self.ended >= 30000:
                self.stopped = True; return actions + ['stop:callEnded']
        return actions
