// SPDX-License-Identifier: GPL-3.0-or-later
// A retained (keepOnNavigation) session whose tab closes while one of its
// page's packets is still awaiting the native reply: the late page ACK hits a
// disconnected port. That is the page's transport going away, not a capture
// failure, so the session must stay listed as closed for the app to Stop.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {setup, until, event} = require('./support/app-control-harness.cjs');
const plain = value => JSON.parse(JSON.stringify(value));

// Controlled time for the closed-session watchdog: timers fire only when the test says.
function fakeTimers(app) {
  const timers = new Map(); let id = 0;
  app.api.sessionTimers.later = (fn, ms) => { timers.set(++id, { fn, ms }); return id; };
  app.api.sessionTimers.cancel = key => { timers.delete(key); };
  return timers;
}

// Native fake that answers start and stop at once and acknowledges everything else.
function nativeFactory() {
  return () => {
    const port = { sent: [], closed: false, onMessage: event(), onDisconnect: event(),
      postMessage(packet) {
        this.sent.push(JSON.parse(JSON.stringify(packet)));
        const reply = packet.type === 'start' ? { type: 'started', path: '/test/Recording-example', features: [] }
          : packet.type === 'stop' ? { type: 'stopped', path: '/test/Recording-example' } : { type: 'ack' };
        queueMicrotask(() => this.onMessage.emit(reply));
      },
      disconnect() { if (!this.closed) { this.closed = true; this.onDisconnect.emit(); } } };
    return port;
  };
}

async function closedRetainedTab() {
  const app = await setup(3, {}, [], nativeFactory());
  const watchdogs = fakeTimers(app);
  const started = await app.command({ type:'command', requestId:'start', action:'start', mode:'tracks', tabId:2, independent:true, keepOnNavigation:true });
  assert.equal(started.ok, true);
  const page = app.pages[0];
  page.postMessage = () => { throw new Error('Attempting to use a disconnected port object.'); };
  page.onDisconnect.emit();
  app.tabs.delete(2); app.chrome.tabs.onRemoved.emit(2);
  assert.deepEqual(plain(app.api.controlStatus().sessions.map(s => [s.tabId, s.pageState, s.stopping])), [[2, 'closed', false]]);
  assert.deepEqual([...watchdogs.values()].map(t => t.ms), [30000]);
  return { app, watchdogs };
}

test('closed tab: if the app never stops it, the watchdog finalizes the session as tabClosed', async () => {
  const { app, watchdogs } = await closedRetainedTab();
  try {
    const [[, timer]] = [...watchdogs];
    watchdogs.clear(); timer.fn(); // 30 s pass with no app Stop and the native port still open.
    await until(() => app.audio[0].sent.some(p => p.type === 'stop') && app.api.controlStatus().sessions.length === 0);
    const stop = app.audio[0].sent.find(p => p.type === 'stop');
    assert.equal(stop.reason, 'tabClosed'); assert.equal(stop.failureReason, undefined); assert.equal(stop.problem, null);
    assert.equal(app.audio[0].closed, true, 'the capture and its session slot are released');
  } finally { app.close(); }
});

test('closed tab: the app\'s own Stop cancels the watchdog, which then never stops anything', async () => {
  const { app, watchdogs } = await closedRetainedTab();
  try {
    const [[, timer]] = [...watchdogs];
    const stopped = await app.command({ type:'command', requestId:'stop', action:'stop', tabId:2, independent:true, reason:'tabClosed' });
    assert.equal(stopped.ok, true);
    assert.equal(watchdogs.size, 0, 'the app Stop cancelled the watchdog');
    timer.fn(); // A stale callback, as if it had already been queued.
    assert.equal(app.audio[0].sent.filter(p => p.type === 'stop').length, 1, 'one Stop, the app\'s');
  } finally { app.close(); }
});

test('closed tab: a native reply arriving after the page port is gone keeps the session closed, not stopped', async () => {
  const held = [];
  // Native fake: start/stop answer at once; every audio-plane packet reply is
  // held until the test releases it (the event the race depends on).
  const native = () => {
    const port = { sent: [], closed: false, onMessage: event(), onDisconnect: event(),
      postMessage(packet) {
        this.sent.push(JSON.parse(JSON.stringify(packet)));
        const reply = packet.type === 'start' ? { type: 'started', path: '/test/Recording-example', features: [] }
          : packet.type === 'stop' ? { type: 'stopped', path: '/test/Recording-example' } : { type: 'ack' };
        if (packet.type === 'start' || packet.type === 'stop') queueMicrotask(() => this.onMessage.emit(reply));
        else held.push(() => this.onMessage.emit(reply));
      },
      disconnect() { if (!this.closed) { this.closed = true; this.onDisconnect.emit(); } } };
    return port;
  };
  const app = await setup(3, {}, [], native);
  const watchdogs = fakeTimers(app);
  try {
    const started = await app.command({ type:'command', requestId:'start', action:'start', mode:'tracks', tabId:2, independent:true, keepOnNavigation:true });
    assert.equal(started.ok, true);
    const page = app.pages[0];
    page.onMessage.emit({ type:'packet', request:1, packet:{ type:'track', id:'remote-1', kind:'remote', sampleRate:48000, channels:1 } });
    await until(() => held.length === 1); // the track packet is in flight to the native host
    // Real tab close: the page port is disconnected (postMessage throws like Chrome), then tabs.onRemoved.
    let acked = 0;
    page.postMessage = () => { acked += 1; throw new Error('Attempting to use a disconnected port object.'); };
    page.onDisconnect.emit();
    app.tabs.delete(2); app.chrome.tabs.onRemoved.emit(2);
    assert.deepEqual(plain(app.api.controlStatus().sessions.map(s => [s.tabId, s.pageState])), [[2, 'closed']]);
    assert.deepEqual([...watchdogs.values()].map(t => t.ms), [30000], 'closed-session watchdog armed, not fired');
    held.shift()(); // the native reply for the in-flight packet arrives now
    const control = app.ports.at(-1);
    // Wait for the reply's continuation itself: its ACK to the gone page.
    await until(() => acked === 1);
    await until(() => control.sent.filter(m => m.type === 'status').length > 0);
    const latest = plain(control.sent.filter(m => m.type === 'status').at(-1));
    assert.equal(app.audio[0].sent.some(p => p.type === 'stop'), false, 'the app owns the closed-tab Stop deadline');
    assert.equal(app.audio[0].closed, false);
    assert.deepEqual(plain(app.api.controlStatus().sessions.map(s => [s.tabId, s.pageState, s.stopping])), [[2, 'closed', false]]);
    assert.ok(latest.sessions.some(s => s.tabId === 2 && s.pageState === 'closed'), 'latest published status still lists the closed tab');
    const stopped = await app.command({ type:'command', requestId:'stop', action:'stop', tabId:2, independent:true, reason:'tabClosed' });
    assert.equal(stopped.ok, true);
    const stop = app.audio[0].sent.find(p => p.type === 'stop');
    assert.equal(stop.reason, 'tabClosed'); assert.equal(stop.failureReason, undefined); assert.equal(stop.problem, null);
  } finally { app.close(); }
});
