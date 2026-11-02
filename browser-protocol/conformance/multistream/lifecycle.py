"""Independent D52 oracle; desktop tests run the literal vectors on their own policy."""
class Policy:
    def __init__(self, source='browser', reason='start', resume=False, generation=None):
        self.source, self.reason = source, reason
        self.heard = self.stopped = False
        self.ended = self.closed = self.missing = None
        # D52.5: resume-capture-v1 negotiated, and the recording's callGeneration.
        # heard latches only on rows of that generation (any generation when None).
        self.resume, self.generation = resume, generation
        self.asked_on = None  # controlConnection of this loss's resume ask

    def lose(self):
        self.source, self.reason, self.asked_on = 'system', 'lost', None
        return ['fallback']

    def step(self, row):
        if self.stopped: return []
        now, page = row['atMs'], row.get('pageState', 'present')
        count = row.get('sourceCount')
        known = row.get('rowPresent', True) and row.get('sourceCountKnown', count is not None)
        generation = row.get('callGeneration')
        # D52.5: another call's heard evidence never arms this recording's
        # restore, OTHERS_GONE grace or resume ask.
        self.heard |= row.get('heard') is True and self.generation in (None, generation)
        # resumeRefused (a refused or failed resume) deliberately changes nothing:
        # no retry on a timer or on later ticks, only on a new controlConnection.
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
            actions = self.lose()
        if page == 'present' and row.get('controlAlive', True) and known and count > 0:
            # D52.4 Q3: a visibly ongoing call stays on fallback without a
            # pageGone deadline; restoring needs a live audio transport.
            self.missing = self.ended = None
            if transport and self.source == 'system' and self.heard:
                action = 'switch:browser:callConnected' if self.reason == 'missing' else 'restore'
                self.source, self.reason, self.asked_on = 'browser', 'restored', None
                return [action]
            connection = row.get('controlConnection', 0)
            if (self.resume and not transport and self.source == 'system' and self.reason == 'lost'
                    and generation == self.generation and self.heard and self.asked_on != connection):
                # D52.5: ask once per loss and control connection; never on a timer.
                self.asked_on = connection
                return actions + ['askResume']
            return actions
        if page != 'present' or not row.get('controlAlive', True) or self.missing is not None:
            self.ended = None
            self.missing = now if self.missing is None else self.missing
            if now - self.missing >= 2000 and self.source != 'system':
                return self.lose()
            return actions
        if not known:
            self.ended = None; return actions
        if self.heard:
            self.ended = now if self.ended is None else self.ended
            if now - self.ended >= 30000:
                self.stopped = True; return actions + ['stop:callEnded']
        return actions
