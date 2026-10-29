// Pure VM fixture: no browser process, AudioContext, device, or filesystem recording.
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
function event() {
  const listeners = new Set();
  return { addListener: fn => listeners.add(fn), removeListener: fn => listeners.delete(fn), emit: (...args) => [...listeners].forEach(fn => fn(...args)) };
}
async function until(check) {
  const deadline = Date.now() + 3000;
  while (!check()) { assert.ok(Date.now() < deadline, 'control fixture timed out'); await new Promise(resolve => setImmediate(resolve)); }
}
async function setup(sourceCount = 3, previous = {}) {
  const ports = [], audio = [], alarms = new Map(), timers = new Map(), pages = [];
  const stored = { ...previous }; let timerID = 0, uuid = 0;
  const tabs = new Map([[2, { id: 2, title: 'Meeting B', url: 'https://meet.example/call' }], [99, { id: 99, title: 'Active unrelated page', url: 'https://other.example/' }]]);
  const getCalls = [], queries = [];
  const chrome = {
    runtime: { id: 'extension', onConnect: event(), onMessage: event(), getURL: name => 'chrome-extension://extension/' + name,
      getContexts: async () => [], connectNative(name) {
        const port = { name, sent: [], closed: false, onMessage: event(), onDisconnect: event(),
          postMessage(packet) {
            this.sent.push(JSON.parse(JSON.stringify(packet)));
            if (name === 'io.codemyriad.cassini') queueMicrotask(() => this.onMessage.emit(packet.type === 'start' ? { type: 'started', path: '/test/Recording-example' } : packet.type === 'stop' ? { type: 'stopped', path: '/test/Recording-example' } : { type: 'ack' }));
          }, disconnect() { if (!this.closed) { this.closed = true; this.onDisconnect.emit(); } }
        };
        (name === 'io.codemyriad.cassini.control' ? ports : audio).push(port); return port;
      }
    },
    alarms: { onAlarm: event(), async create(name, value) { alarms.set(name, value); }, async clear(name) { alarms.delete(name); } },
    storage: { session: { async get() { return { ...stored }; }, async set(value) { Object.assign(stored, value); }, async remove(key) { delete stored[key]; } } },
    tabs: { onRemoved: event(), onUpdated: event(), async get(id) { getCalls.push(id); if (!tabs.has(id)) throw Error('Missing tab'); return { ...tabs.get(id) }; },
      async query(query) { queries.push(query); return query.active ? [tabs.get(99)] : [...tabs.values()]; }, async sendMessage() { return {}; }
    },
    action: { async setBadgeText() {}, async setBadgeBackgroundColor() {} },
    tabCapture: { async getMediaStreamId() { throw Error('No gesture in control fixtures'); } },
    offscreen: { async closeDocument() {} }
  };
  const later = (fn, ms) => { const id = ++timerID; timers.set(id, { fn, ms }); return id; };
  const cancel = id => timers.delete(id);
  const context = vm.createContext({ chrome, URL, crypto: { randomUUID: () => 'token-' + (++uuid) }, queueMicrotask, setTimeout, clearTimeout });
  const extension = path.resolve(__dirname, '../..');
  vm.runInContext(fs.readFileSync(path.join(extension, 'app-control.js'), 'utf8').replace('export class AppControl', 'class AppControl'), context);
  context.controlLater = later; context.controlCancel = cancel;
  const background = fs.readFileSync(path.join(extension, 'background.js'), 'utf8').replace(/^import .*\n/, '').replace('runtime: chrome.runtime, alarms: chrome.alarms,', 'runtime: chrome.runtime, alarms: chrome.alarms, later: controlLater, cancel: controlCancel,');
  vm.runInContext(background + '\nglobalThis.api={status,controlStatus,appControl,frames,start,stop,meetings};', context);
  await until(() => ports.length && ports[0].sent.some(m => m.type === 'status'));
  function frame(tabId = 2, count = sourceCount) {
    const port = { name: 'cassini-frame', sender: { id: 'extension', tab: { id: tabId } }, onMessage: event(), onDisconnect: event(), sent: [],
      postMessage(message) { this.sent.push(message); if (message.type === 'stop') queueMicrotask(() => this.onMessage.emit({ type: 'stopped' })); } };
    chrome.runtime.onConnect.emit(port); port.onMessage.emit({ type: 'discovery', sourceCount: count, title: 'Spoofed title', url: 'https://spoofed.invalid/' }); pages.push(port); return port;
  }
  frame();
  await context.api.appControl.publishing;
  async function command(message) {
    const port = context.api.appControl.port; const before = port.sent.length;
    port.onMessage.emit(message);
    await until(() => port.sent.slice(before).some(m => m.type === 'result' && m.requestId === message.requestId));
    return port.sent.slice(before).find(m => m.type === 'result' && m.requestId === message.requestId);
  }
  return { api: context.api, chrome, ports, audio, timers, alarms, pages, tabs, getCalls, queries, command, frame,
    async reconnect() { const before = ports.length; ports.at(-1).disconnect(); const timer = [...timers.values()][0]; assert.ok(timer); timers.clear(); timer.fn(); await until(() => ports.length > before && ports.at(-1).sent.some(m => m.type === 'status')); return ports.at(-1); },
    close() { context.api.appControl.close(); }
  };
}
async function runCase(testCase) {
  const app = await setup(testCase.sourceCount ?? 3);
  try {
    const initial = app.ports[0].sent;
    assert.equal(initial[0].type, 'hello'); assert.equal(initial[0].version, 1);
    assert.equal(initial[1].type, 'meetings'); assert.equal(initial[2].type, 'status');
    const results = [];
    // Send duplicate requests concurrently so the cache must also cover pending work.
    if (testCase.name === 'duplicate-request-id') results.push(...await Promise.all(testCase.commands.map(c => app.command(c))));
    else for (const command of testCase.commands || []) results.push(await app.command(command));
    for (let i = 0; i < (testCase.expect || []).length; i++) {
      const expected = testCase.expect[i], actual = results[i]; assert.equal(actual.ok, expected.ok);
      if (expected.error) assert.equal(actual.error, expected.error);
      if (expected.ok) { assert.equal(actual.status.recording, expected.recording); assert.equal(actual.status.tabId, expected.tabId); }
    }
    if (testCase.commands?.[0]?.action === 'start' && results[0]?.ok) {
      assert.equal(app.audio[0].sent[0].title, 'Meeting B');
      assert.equal(app.getCalls.includes(2), true);
      assert.ok(!app.queries.some(query => query.active), 'app start must never query the active tab');
    }
    if (testCase.audioStarts !== undefined) assert.equal(app.audio.length, testCase.audioStarts);
    if (testCase.name === 'duplicate-request-id') assert.equal(app.ports[0].sent.filter(m => m.type === 'result' && m.requestId === 'record').length, 2);
    if (testCase.kind === 'disconnect' || testCase.kind === 'reconnect') {
      app.ports.at(-1).disconnect(); assert.equal(app.api.status().recording, true); assert.equal(app.audio[0].closed, false);
      assert.ok(!app.audio[0].sent.some(m => m.type === 'stop'));
      if (testCase.kind === 'reconnect') {
        const next = await app.reconnect(); const state = next.sent.find(m => m.type === 'status');
        assert.equal(state.recording, true); assert.equal(state.tabId, 2); assert.equal(state.path, '/test/Recording-example');
      }
    }
    if (testCase.kind === 'discovery') {
      const snapshot = await app.api.meetings(); assert.equal(snapshot.meetings[0].title, 'Meeting B'); assert.equal(snapshot.meetings[0].url, 'https://meet.example/call');
      assert.equal(app.audio.length, 0);
      const count = snapshot.meetings[0].sourceCount;
      for (const sourceCount of [-1, 65, '2', NaN]) app.pages[0].onMessage.emit({ type: 'discovery', sourceCount });
      assert.equal((await app.api.meetings()).meetings[0].sourceCount, count);
      app.chrome.tabs.onUpdated.emit(2, { status: 'loading' }); assert.equal((await app.api.meetings()).meetings.length, 0);
      app.pages[0].onMessage.emit({ type: 'discovery', sourceCount: 2 }); app.pages[0].onDisconnect.emit();
      assert.equal((await app.api.meetings()).meetings.length, 0);
    }
    return { initial, results };
  } finally { app.close(); }
}
module.exports = { setup, runCase, event, until };
