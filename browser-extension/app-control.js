// Persistent app management, independent of the per-recording PCM connection.
export class AppControl {
  constructor({ runtime, alarms, snapshot, command, connected = async () => {},
                later = (fn, ms) => setTimeout(fn, ms), cancel = id => clearTimeout(id) }) {
    Object.assign(this, { runtime, alarms, snapshot, command, connected, later, cancel });
    this.port = null;
    this.retry = null;
    this.delay = 1000;
    this.stopped = false;
    this.requests = new Map();
    this.completed = new Set();
    this.tail = Promise.resolve();
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
    const port = this.port; this.port = null;
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
    if (Date.now() - this.connectedAt >= 30000) this.delay = 1000;
    try { port.disconnect(); } catch {}
    this.requests.clear(); this.completed.clear();
    this.reconnect();
  }
  connect() {
    if (this.stopped || this.port) return;
    this.cancel(this.retry); this.retry = null;
    let port;
    try { port = this.runtime.connectNative('com.codemyriad.cassini.control'); }
    catch { this.reconnect(); return; }
    this.port = port;
    this.connectedAt = Date.now();
    this.requests.clear(); this.completed.clear();
    port.onDisconnect.addListener(() => { void this.runtime.lastError; this.disconnected(port); });
    port.onMessage.addListener(message => this.receive(port, message));
    this.send(port, { type: 'hello', version: 1 });
    Promise.resolve().then(() => this.connected()).then(() => {
      if (port !== this.port) return;
      this.alarms.clear('cassini-app-control').catch(() => {});
      this.publish();
    }).catch(() => this.publish());
  }
  publish() {
    this.dirty = true;
    if (!this.port || this.publishing) return;
    this.publishing = (async () => {
      while (this.dirty && this.port) {
        this.dirty = false;
        const port = this.port;
        try { for (const message of await this.snapshot()) this.send(port, message); }
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
      const execute = async () => {
        if (port !== this.port) return null;
        try {
          if (!['start', 'stop'].includes(message.action) || !Number.isSafeInteger(message.tabId) || message.tabId < 0)
            throw new Error('Choose a meeting to record.');
          if (message.action === 'start' && message.mode !== 'tracks')
            throw new Error('To record the whole tab, click Record the whole tab in the browser.');
          const status = await this.command(message);
          return { type: 'result', requestId: id, ok: true, status };
        } catch (error) {
          return { type: 'result', requestId: id, ok: false, error: String(error.message || error).slice(0, 2000) };
        }
      };
      result = this.tail.then(execute, execute);
      this.tail = result.catch(() => {});
      this.requests.set(key, result);
      result.then(() => { if (port === this.port && this.requests.get(key) === result) this.completed.add(key); });
    }
    result.then(value => { if (value) this.send(port, value); });
  }
}
