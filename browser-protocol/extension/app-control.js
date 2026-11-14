// SPDX-License-Identifier: GPL-3.0-or-later
// Persistent app management, independent of the per-recording PCM connection.
export class AppControl {
  constructor({ runtime, alarms, snapshot, command, capabilities = [], identity = () => ({}), cancelPending = () => {}, connected = async () => {},
                later = (fn, ms) => setTimeout(fn, ms), cancel = id => clearTimeout(id), busy = () => false, signalSnapshot = null, signalFinalize = value => value,
               recordWaiting = () => {} }) {
    Object.assign(this, { runtime, alarms, snapshot, command, capabilities, identity, cancelPending, connected, later, cancel, busy, signalSnapshot, signalFinalize, recordWaiting });
    this.port = null;
    this.resetSignals();
    this.ready = false;
    this.connecting = false;
    this.retry = null;
    this.delay = 1000;
    this.stopped = false;
    this.requests = new Map();
    this.completed = new Set();
    this.resumeIntents = new Map();
    this.tail = Promise.resolve();
    this.tabTails = new Map();
    // popup-record-v1: popup Start/Stop asks the app, which keeps owning policy.
    this.recordConfigured = false;
    this.recordRequests = new Map();
    this.recordSequence = 0;
    // Bumped whenever popup asks lose their footing (disconnect, reconnect,
    // revocation, close) so accepted-but-unreflected asks stop claiming progress.
    this.recordEpoch = 0;
    this.alarm = alarm => { if (alarm.name === 'cassini-app-control') this.connect(); };
  }
  start() {
    this.alarms.onAlarm.addListener(this.alarm);
    this.connect();
  }
  close() {
    this.stopped = true;
    this.resumeIntents.clear();
    this.failRecordRequests();
    this.cancel(this.retry);
    this.alarms.onAlarm.removeListener(this.alarm);
    this.alarms.clear('cassini-app-control').catch(() => {});
    const port = this.port; this.port = null; this.resetSignals(); this.ready = false; this.connecting = false;
    port?.disconnect();
  }
  reconnect() {
    if (this.stopped || this.retry !== null) return;
    // Alarms also wake an idle MV3 worker after its short-lived timer disappears.
    this.alarms.create('cassini-app-control', { delayInMinutes: 1, periodInMinutes: 1 }).catch(() => {});
    this.retry = this.later(() => { this.retry = null; this.connect(); }, this.delay);
    this.delay = Math.min(this.delay * 2, 30000);
  }
  send(port, message) {
    if (port !== this.port) return;
    try { port.postMessage(message); }
    catch { this.disconnected(port); }
  }
  disconnected(port) {
    if (port !== this.port) return;
    this.port = null;
    this.resetSignals();
    this.ready = false;
    this.connecting = false;
    if (Date.now() - this.connectedAt >= 30000) this.delay = 1000;
    try { port.disconnect(); } catch {}
    this.requests.clear(); this.completed.clear();
    this.resumeIntents.clear();
    this.failRecordRequests();
    this.reconnect();
  }
  connect() {
    if (this.stopped || this.port) return;
    this.cancel(this.retry); this.retry = null;
    let port;
    try { port = this.runtime.connectNative('io.codemyriad.cassini.control'); }
    catch { this.reconnect(); return; }
    this.port = port;
    this.resetSignals();
    this.ready = false;
    this.connecting = true;
    this.tabTails.clear();
    this.failRecordRequests();
    this.connectedAt = Date.now();
    this.requests.clear(); this.completed.clear();
    port.onDisconnect.addListener(() => { void this.runtime.lastError; this.disconnected(port); });
    port.onMessage.addListener(message => this.receive(port, message));
    const hello = { type: 'hello', version: 1, ...this.identity() };
    if (this.capabilities.length) hello.capabilities = this.capabilities;
    this.send(port, hello);
    Promise.resolve().then(() => this.connected()).then(() => {
      if (port !== this.port) return;
      this.connecting = false;
      this.alarms.clear('cassini-app-control').catch(() => {});
      this.publish();
    }).catch(() => {
      if (port !== this.port) return;
      this.connecting = false;
      this.publish();
    });
  }
  publishStatus(message) {
    // Lifecycle edges must not wait behind an asynchronous inventory refresh.
    const port = this.port;
    if (port && this.ready) this.send(port, message);
  }
  publish() {
    this.publishSignals();
    this.dirty = true;
    if (!this.port || this.connecting || this.publishing) return;
    this.publishing = (async () => {
      while (this.dirty && this.port && !this.connecting) {
        this.dirty = false;
        const port = this.port, epoch = this.signalEpoch;
        try {
          const messages = await this.snapshot({verified:this.verifiedConfigured});
          // A configure/revoke crossing an asynchronous inventory lookup must
          // not publish aliases belonging to the previous negotiated shape.
          if (epoch !== this.signalEpoch) { this.dirty = true; continue; }
          for (const message of messages) this.send(port, message);
          if (port === this.port) this.ready = true;
        }
        catch { /* A failed metadata refresh must never stop audio capture. */ }
      }
    })().finally(() => { this.publishing = null; if (this.dirty && this.port) this.publish(); });
  }
  resetSignals() {
    this.cancel(this.signalTimer);this.signalTimer=null;
    this.signalConnectionEpoch=(this.signalConnectionEpoch||0)+1;
    this.signalsConfigured = false;
    this.verifiedConfigured = false;
    this.signalEpoch = (this.signalEpoch || 0) + 1;
    this.signalSequence = 0;
    this.signalRun = null;
    this.signalDirty = false;
    this.lastSignals = null;
  }
  publishSignals() {
    if (!this.port || !this.signalsConfigured) return;
    this.signalDirty = true;
    if (this.connecting || this.signalRun) return;
    const run = this.signalRun = {startedAt:Date.now()};
    void (async () => {
      while (this.signalRun === run && this.signalDirty && this.port && this.signalsConfigured && !this.connecting) {
        this.signalDirty = false;
        const port = this.port, epoch = this.signalEpoch;
        try {
          let snapshot = await this.signalSnapshot({verified:this.verifiedConfigured,epoch:this.signalConnectionEpoch,configurationEpoch:this.signalEpoch});
          if (this.signalRun !== run || port !== this.port || epoch !== this.signalEpoch || !this.signalsConfigured) break;
          if (!snapshot) continue;
          snapshot=this.signalFinalize(snapshot,{verified:this.verifiedConfigured});
          if (!snapshot) continue;
          const serialized = JSON.stringify(snapshot);
          if (serialized === this.lastSignals && !this.verifiedConfigured) continue;
          this.lastSignals = serialized;
          this.send(port,{type:'callSignals',sequence:this.signalSequence++,...snapshot});
        } catch { /* Failed metadata is not an audio failure or a capture command. */ }
      }
    })().finally(() => {
      if (this.signalRun !== run) return;
      this.signalRun = null;
      if (this.signalDirty && this.signalsConfigured) this.publishSignals();
      else if (this.verifiedConfigured && this.port && this.signalTimer==null) this.signalTimer=this.later(()=>{
        this.signalTimer=null;this.publishSignals();
      },Math.max(0,500-(Date.now()-run.startedAt)));
    });
  }
  // What the popup can offer: the app must be connected, past its initial
  // snapshot, and have opted in to popup-record-v1 on this connection.
  recordAvailability() {
    if (!this.port || !this.ready) return 'disconnected';
    return this.recordConfigured ? 'available' : 'unsupported';
  }
  failRecordRequests() {
    this.recordConfigured = false;
    this.recordEpoch++;
    const pending = [...this.recordRequests.values()];
    this.recordRequests.clear();
    for (const request of pending) { this.cancel(request.timer); request.reject(request.lost()); }
  }
  recordUnavailable() {
    const unavailable = this.recordAvailability();
    if (unavailable === 'disconnected') return Object.assign(new Error('Cassini is not connected. Open Cassini, then try again.'), { code: 'appUnavailable' });
    if (unavailable === 'unsupported') return Object.assign(new Error('This version of Cassini cannot start or stop recordings from the browser. Use the Cassini app.'), { code: 'unsupported' });
    return null;
  }
  // Waits until every inventory/status change notified so far has been sent on
  // this connection, so an ask never overtakes the snapshot that introduces its
  // tab, callGeneration or captureId. Stops as soon as `live()` turns false; the
  // caller's deadline, not this loop, bounds a stalled or constantly busy page.
  async flushPublication(live = () => true) {
    while (live() && this.port && !this.connecting && (this.dirty || this.publishing)) {
      if (!this.publishing) this.publish();
      await this.publishing;
    }
  }
  // Sends one popup ask and resolves with the app's correlated answer. The app
  // performs any recording through its own ordinary control command. The ask is
  // registered (deadline and disconnect/revocation cancellation armed) before
  // pending publication is flushed; `build` then runs synchronously, so the ask
  // carries the same state the app was just told about. It may throw a refusal.
  requestRecording(build, timeoutMs = 10000) {
    const unavailable = this.recordUnavailable();
    if (unavailable) return Promise.reject(unavailable);
    const port = this.port, epoch = this.recordEpoch;
    const requestId = `popup-${this.connectedAt}-${++this.recordSequence}`;
    let request;
    const answer = new Promise((resolve, reject) => {
      request = { resolve, reject,
        lost: () => Object.assign(new Error('Cassini disconnected before answering. Check the Cassini app.'), { code: 'appUnavailable' }) };
    });
    const live = () => this.recordRequests.get(requestId) === request;
    const fail = error => {
      if (!live()) return;
      this.recordRequests.delete(requestId);
      this.cancel(request.timer);
      request.reject(error);
    };
    request.timer = this.later(() => fail(Object.assign(new Error('Cassini did not answer in time. Check the Cassini app.'), { code: 'timeout' })), timeoutMs);
    this.recordRequests.set(requestId, request);
    this.recordWaiting(requestId);
    void (async () => {
      await this.flushPublication(live);
      // Timed out, disconnected or revoked meanwhile: the ask is never sent.
      if (!live()) return;
      if (port !== this.port || epoch !== this.recordEpoch) return fail(request.lost());
      const refused = this.recordUnavailable();
      if (refused) return fail(refused);
      const body = typeof build === 'function' ? build() : build;
      this.send(port, { type: 'recordRequest', requestId, ...body });
    })().catch(fail);
    return answer;
  }
  receive(port, message) {
    if (port !== this.port) return;
    if (message?.type === 'recordRequests') {
      if (typeof message.enabled !== 'boolean' || Object.keys(message).some(key => !['type','enabled'].includes(key))) return;
      const enabled = message.enabled && this.capabilities.includes('popup-record-v1');
      if (!enabled) this.failRecordRequests();
      this.recordConfigured = enabled;
      this.send(port, { type: 'recordRequestsConfigured', enabled });
      return;
    }
    if (message?.type === 'recordRequestResult') {
      const request = typeof message.requestId === 'string' ? this.recordRequests.get(message.requestId) : undefined;
      if (!request || typeof message.ok !== 'boolean') return;
      this.recordRequests.delete(message.requestId);
      this.cancel(request.timer);
      if (message.ok) request.resolve({ ok: true });
      else request.resolve({ ok: false,
        error: typeof message.error === 'string' && message.error ? [...message.error].slice(0, 2000).join('') : 'Cassini could not do that. Check the Cassini app.',
        ...(typeof message.code === 'string' && /^[A-Za-z]{1,32}$/.test(message.code) ? { code: message.code } : {}) });
      return;
    }
    if (message?.type === 'configure') {
      const values = message.capabilities;
      if (!Array.isArray(values) || values.length > 2 || new Set(values).size !== values.length ||
          values.some(value => !['call-signals-v1','verified-auto-admission-v1'].includes(value)) ||
          (values.includes('verified-auto-admission-v1') && !values.includes('call-signals-v1')) ||
          Object.keys(message).some(key => !['type','capabilities'].includes(key))) return;
      this.cancel(this.signalTimer);this.signalTimer=null;
      this.signalEpoch++;
      this.signalRun = null;
      this.lastSignals = null;
      this.signalsConfigured = values.includes('call-signals-v1') && this.capabilities.includes('call-signals-v1') && typeof this.signalSnapshot === 'function';
      this.verifiedConfigured = this.signalsConfigured && values.includes('verified-auto-admission-v1') && this.capabilities.includes('verified-auto-admission-v1');
      this.signalDirty = false;
      this.send(port,{type:'configured',capabilities:this.signalsConfigured ? ['call-signals-v1',...(this.verifiedConfigured ? ['verified-auto-admission-v1'] : [])] : []});
      this.publish();
      return;
    }
    // heartbeat-v1: answer at once, never through the command queues (a Stop
    // flush can legitimately hold them longer than the app's loss deadline).
    // A pong is only ever a reply; the extension sends no unsolicited types.
    if (message?.type === 'ping') {
      if (!this.capabilities.includes('heartbeat-v1') || !Number.isSafeInteger(message.seq) || message.seq < 0) return;
      this.delay = 1000;
      this.send(port, { type: 'pong', seq: message.seq });
      return;
    }
    if (message?.type !== 'command') return;
    this.delay = 1000;
    const id = message.requestId;
    if (!(typeof id === 'string' && id.length > 0 && [...id].length <= 128)
        && !(Number.isSafeInteger(id) && id >= 0)) return;
    const key = JSON.stringify(id);
    let result = this.requests.get(key);
    if (!result) {
      if (this.requests.size >= 128 && this.completed.size) {
        const oldest = this.completed.values().next().value;
        this.completed.delete(oldest); this.requests.delete(oldest);
      }
      if (this.requests.size >= 128) {
        // Never evict an in-flight command and accidentally execute its retry twice.
        // D58 #11: a Start for a busy tab says so even when the table is full.
        this.send(port, this.busy(message)
          ? { type: 'result', requestId: id, ok: false, error: 'A recording is already running. Stop it before starting another.', code: 'busy' }
          : { type: 'result', requestId: id, ok: false, error: 'Cassini has received too many commands. Reconnect and try again.', code: 'limit' });
        return;
      }
      const resume = message.resumeCapture;
      const validResume = resume === undefined || this.capabilities.includes('resume-capture-v1') &&
        message.action === 'start' && message.independent === true && resume !== null && typeof resume === 'object' &&
        typeof resume.captureId === 'string' && resume.captureId.length > 0 &&
        Array.from(resume.captureId).reduce((n,c)=>n+(c.codePointAt(0)<=127?1:c.codePointAt(0)<=2047?2:c.codePointAt(0)<=65535?3:4),0)<=128 &&
        Number.isSafeInteger(resume.timelineOriginMs) && resume.timelineOriginMs >= 0 && resume.timelineOriginMs <= 253402300799000 &&
        typeof message.callGeneration === 'string' && message.callGeneration.length > 0;
      const validOptions = validResume && ['independent', 'captureLocal', 'microphone', 'keepOnNavigation'].every(key => message[key] === undefined || typeof message[key] === 'boolean')
        && (message.keepOnNavigation !== true || (message.action === 'start' && message.independent === true))
        && (message.callGeneration === undefined || (message.action === 'start' && typeof message.callGeneration === 'string' && message.callGeneration.length > 0 && message.callGeneration.length <= 200))
        && (message.captureId === undefined || (message.action === 'stop' && typeof message.captureId === 'string' && message.captureId.length > 0 &&
            Array.from(message.captureId).reduce((n,c)=>n+(c.codePointAt(0)<=127?1:c.codePointAt(0)<=2047?2:c.codePointAt(0)<=65535?3:4),0)<=200))
        && (message.reason === undefined || (message.action === 'stop' &&
            ['user', 'callEnded', 'tabClosed', 'pageGone', 'disk', 'error', 'appQuit'].includes(message.reason)));
      const intent = { message, cancelled: false, cancelledResume: false };
      if (validOptions && resume !== undefined) this.resumeIntents.set(key, intent);
      if (message.independent === true && message.action === 'stop' && Number.isSafeInteger(message.tabId) && message.tabId >= 0 && validOptions) {
        for (const pending of this.resumeIntents.values()) {
          if (pending.message.tabId === message.tabId && (message.captureId === undefined || message.captureId === pending.message.resumeCapture.captureId)) {
            pending.cancelled = true;
            intent.cancelledResume = true;
          }
        }
        this.cancelPending(message);
      }
      // D58: `resultCode` carries the open result `code` (SPEC-CONTROL "Result
      // codes"). A Start this app cancelled itself is not classified.
      const invalid = message => Object.assign(new Error(message), { resultCode: 'invalid' });
      const execute = async () => {
        if (port !== this.port) return null;
        try {
          // D58 #11: `busy` wins over every other refusal of a Start for that tab.
          if (this.busy(message)) throw Object.assign(new Error('A recording is already running. Stop it before starting another.'), { resultCode: 'busy' });
          if ((!['start', 'stop'].includes(message.action) && !(message.action === 'reloadTab' && this.capabilities.includes('tab-readiness-v1'))) || !Number.isSafeInteger(message.tabId) || message.tabId < 0)
            throw invalid('Choose a meeting to record.');
          if (message.action === 'start' && message.mode !== 'tracks')
            throw invalid('To record the whole tab, click Record the whole tab in the browser.');
          for (const key of ['independent', 'captureLocal', 'microphone', 'keepOnNavigation']) {
            if (message[key] !== undefined && typeof message[key] !== 'boolean')
              throw invalid('Choose a meeting to record.');
          }
          if (!validOptions) throw invalid('Choose a meeting to record.');
          if (intent.cancelled) throw new Error('Recording could not start.');
          const status = await this.command(message, intent);
          return { type: 'result', requestId: id, ok: true, status };
        } catch (error) {
          const result = { type: 'result', requestId: id, ok: false, error: String(error.message || error).slice(0, 2000) };
          if (typeof error?.resultCode === 'string' && /^[A-Za-z]{1,32}$/.test(error.resultCode)) result.code = error.resultCode;
          return result;
        }
      };
      // Independent tabs must not wait on another tab's lookup, audio host or
      // flush. Same-tab commands remain ordered; a legacy command is a global
      // barrier, preserving the original single-slot arrival-order behavior.
      const independent = message.independent === true && Number.isSafeInteger(message.tabId)
        && message.tabId >= 0 && validOptions;
      const predecessors = independent
        ? [this.tail, this.tabTails.get(message.tabId)]
        : [this.tail, ...this.tabTails.values()];
      result = Promise.all(predecessors).then(execute, execute);
      const tail = result.catch(() => {});
      if (independent) {
        this.tabTails.set(message.tabId, tail);
        tail.then(() => { if (this.tabTails.get(message.tabId) === tail) this.tabTails.delete(message.tabId); });
      } else this.tail = tail;
      this.requests.set(key, result);
      result.finally(() => { if (this.resumeIntents.get(key) === intent) this.resumeIntents.delete(key); });
      result.then(() => { if (port === this.port && this.requests.get(key) === result) this.completed.add(key); });
    }
    result.then(value => { if (value) this.send(port, value); });
  }
}
