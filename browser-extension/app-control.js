// SPDX-License-Identifier: GPL-3.0-or-later
// Persistent app management, independent of the per-recording PCM connection.
export class AppControl {
  constructor({ runtime, alarms, snapshot, command, capabilities = [], cancelPending = () => {}, connected = async () => {},
                later = (fn, ms) => setTimeout(fn, ms), cancel = id => clearTimeout(id) }) {
    Object.assign(this, { runtime, alarms, snapshot, command, capabilities, cancelPending, connected, later, cancel });
    this.port = null;
    this.ready = false;
    this.retry = null;
    this.delay = 1000;
    this.stopped = false;
    this.requests = new Map();
    this.completed = new Set();
    this.tail = Promise.resolve();
    this.tabTails = new Map();
    this.alarm = alarm => { if (alarm.name === 'cassini-app-control') this.connect(); };
  }
  start() {
    this.alarms.onAlarm.addListener(this.alarm);
    this.connect();
  }
  close() {
    this.stopped = true;
    this.cancel(this.retry);
    this.alarms.onAlarm.removeListener(this.alarm);
    this.alarms.clear('cassini-app-control').catch(() => {});
    const port = this.port; this.port = null; this.ready = false;
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
    this.ready = false;
    if (Date.now() - this.connectedAt >= 30000) this.delay = 1000;
    try { port.disconnect(); } catch {}
    this.requests.clear(); this.completed.clear();
    this.reconnect();
  }
  connect() {
    if (this.stopped || this.port) return;
    this.cancel(this.retry); this.retry = null;
    let port;
    try { port = this.runtime.connectNative('io.codemyriad.cassini.control'); }
    catch { this.reconnect(); return; }
    this.port = port;
    this.ready = false;
    this.tabTails.clear();
    this.connectedAt = Date.now();
    this.requests.clear(); this.completed.clear();
    port.onDisconnect.addListener(() => { void this.runtime.lastError; this.disconnected(port); });
    port.onMessage.addListener(message => this.receive(port, message));
    const hello = { type: 'hello', version: 1 };
    if (this.capabilities.length) hello.capabilities = this.capabilities;
    this.send(port, hello);
    Promise.resolve().then(() => this.connected()).then(() => {
      if (port !== this.port) return;
      this.alarms.clear('cassini-app-control').catch(() => {});
      this.publish();
    }).catch(() => this.publish());
  }
  publishStatus(message) {
    // Lifecycle edges must not wait behind an asynchronous inventory refresh.
    const port = this.port;
    if (port && this.ready) this.send(port, message);
  }
  publish() {
    this.dirty = true;
    if (!this.port || this.publishing) return;
    this.publishing = (async () => {
      while (this.dirty && this.port) {
        this.dirty = false;
        const port = this.port;
        try {
          for (const message of await this.snapshot()) this.send(port, message);
          if (port === this.port) this.ready = true;
        }
        catch { /* A failed metadata refresh must never stop audio capture. */ }
      }
    })().finally(() => { this.publishing = null; if (this.dirty && this.port) this.publish(); });
  }
  receive(port, message) {
    if (port !== this.port || message?.type !== 'command') return;
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
        this.send(port, { type: 'result', requestId: id, ok: false, error: 'Cassini has received too many commands. Reconnect and try again.' });
        return;
      }
      const validOptions = ['independent', 'captureLocal', 'microphone', 'keepOnNavigation'].every(key => message[key] === undefined || typeof message[key] === 'boolean')
        && (message.keepOnNavigation !== true || (message.action === 'start' && message.independent === true))
        && (message.reason === undefined || (message.action === 'stop' &&
            ['user', 'callEnded', 'tabClosed', 'pageGone', 'disk', 'error', 'appQuit'].includes(message.reason)));
      if (message.independent === true && message.action === 'stop' && Number.isSafeInteger(message.tabId) && message.tabId >= 0 && validOptions)
        this.cancelPending(message);
      const execute = async () => {
        if (port !== this.port) return null;
        try {
          if (!['start', 'stop'].includes(message.action) || !Number.isSafeInteger(message.tabId) || message.tabId < 0)
            throw new Error('Choose a meeting to record.');
          if (message.action === 'start' && message.mode !== 'tracks')
            throw new Error('To record the whole tab, click Record the whole tab in the browser.');
          for (const key of ['independent', 'captureLocal', 'microphone', 'keepOnNavigation']) {
            if (message[key] !== undefined && typeof message[key] !== 'boolean')
              throw new Error('Choose a meeting to record.');
          }
          if (!validOptions) throw new Error('Choose a meeting to record.');
          const status = await this.command(message);
          return { type: 'result', requestId: id, ok: true, status };
        } catch (error) {
          return { type: 'result', requestId: id, ok: false, error: String(error.message || error).slice(0, 2000) };
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
      result.then(() => { if (port === this.port && this.requests.get(key) === result) this.completed.add(key); });
    }
    result.then(value => { if (value) this.send(port, value); });
  }
}
