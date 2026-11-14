// popup-record-v1: the popup's Start/Stop asks the connected app over the
// control port; the app records through its ordinary command. VM fixture only.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { setup, until } = require('./support/app-control-harness.cjs');

const popup = { id: 'extension', url: 'chrome-extension://extension/popup.html' };
function rpc(app, message, sender = popup) {
  return new Promise(resolve => app.chrome.runtime.onMessage.emit({ target: 'background', ...message }, sender, resolve));
}
const plain = value => JSON.parse(JSON.stringify(value));
const sent = (port, type) => port.sent.filter(message => message.type === type);
async function optIn(app, enabled = true) {
  const port = app.ports.at(-1);
  port.onMessage.emit({ type: 'recordRequests', enabled });
  await until(() => sent(port, 'recordRequestsConfigured').length);
  return sent(port, 'recordRequestsConfigured').at(-1);
}
// Resolves when the next popup ask enters its publication wait (event gate, no turn counting).
function waiting(control) {
  return new Promise(resolve => { control.recordWaiting = requestId => { control.recordWaiting = () => {}; resolve(requestId); }; });
}
async function asked(app, count = 1) {
  const port = app.ports.at(-1);
  await until(() => sent(port, 'recordRequest').length >= count);
  return sent(port, 'recordRequest')[count - 1];
}

test('popup record: hello advertises the capability; requests need the app to opt in on this connection', async () => {
  const app = await setup();
  try {
    assert.ok(app.ports[0].sent[0].capabilities.includes('popup-record-v1'));
    let reply = await rpc(app, { type: 'status', tabId: 2 });
    assert.equal(reply.appRecording, 'unsupported');
    reply = await rpc(app, { type: 'recordRequest', action: 'start', tabId: 2 });
    assert.equal(reply.code, 'unsupported');
    assert.match(reply.error, /cannot start or stop recordings from the browser/);
    assert.equal(sent(app.ports[0], 'recordRequest').length, 0);
    // Malformed opt-ins are ignored without an acknowledgement.
    for (const bad of [{ type: 'recordRequests' }, { type: 'recordRequests', enabled: 'yes' }, { type: 'recordRequests', enabled: true, extra: 1 }])
      app.ports[0].onMessage.emit(bad);
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(sent(app.ports[0], 'recordRequestsConfigured').length, 0);
    assert.deepEqual(plain(await optIn(app)), { type: 'recordRequestsConfigured', enabled: true });
    assert.equal((await rpc(app, { type: 'status', tabId: 2 })).appRecording, 'available');
    const next = await app.reconnect();
    assert.equal((await rpc(app, { type: 'status', tabId: 2 })).appRecording, 'unsupported', 'reconnect starts without opt-in');
    assert.equal(sent(next, 'recordRequestsConfigured').length, 0);
    assert.deepEqual(plain(await optIn(app, false)), { type: 'recordRequestsConfigured', enabled: false });
  } finally { app.close(); }
});

test('popup record: Start asks the app, which records through its own command', async () => {
  const app = await setup();
  try {
    await optIn(app);
    const reply = rpc(app, { type: 'recordRequest', action: 'start', tabId: 2 });
    const ask = await asked(app);
    assert.deepEqual(Object.keys(ask).sort(), ['action', 'callGeneration', 'pageState', 'requestId', 'tabId', 'title', 'type', 'url']);
    assert.equal(ask.action, 'start'); assert.equal(ask.tabId, 2);
    assert.equal(ask.callGeneration, app.api.callInfo(2).generation);
    assert.equal(app.audio.length, 0, 'the extension never starts native capture for the ask itself');
    const pending = await rpc(app, { type: 'status', tabId: 2 });
    assert.deepEqual(plain(pending.recordRequest), { action: 'start', state: 'pending' });
    assert.equal((await rpc(app, { type: 'recordRequest', action: 'start', tabId: 2 })).code, 'busy', 'one ask per tab at a time');
    app.ports[0].onMessage.emit({ type: 'recordRequestResult', requestId: 'someone-else', ok: false, error: 'ignored' });
    app.ports[0].onMessage.emit({ type: 'recordRequestResult', requestId: ask.requestId, ok: true });
    const accepted = await reply;
    assert.equal(accepted.error, undefined);
    assert.deepEqual(plain(accepted.recordRequest), { action: 'start', state: 'accepted' });
    // The app's own manual Start: its options, its requestId.
    const result = await app.command({ type: 'command', requestId: 'app-manual-1', action: 'start', tabId: 2, mode: 'tracks', independent: true, keepOnNavigation: true, callGeneration: ask.callGeneration });
    assert.equal(result.ok, true);
    assert.equal(app.audio.at(-1).sent[0].requestId, 'app-manual-1');
    const recording = await rpc(app, { type: 'status', tabId: 2 });
    assert.equal(recording.recording, true);
    assert.equal(recording.recordRequest, undefined, 'the accepted ask clears once capture reflects it');
    // Stop names the running capture, and the app stops it with its own command.
    const stopReply = rpc(app, { type: 'recordRequest', action: 'stop', tabId: 2 });
    const stopAsk = await asked(app, 2);
    assert.deepEqual(plain(stopAsk), { type: 'recordRequest', requestId: stopAsk.requestId, action: 'stop', tabId: 2, captureId: recording.captureId });
    assert.notEqual(stopAsk.requestId, ask.requestId);
    app.ports[0].onMessage.emit({ type: 'recordRequestResult', requestId: stopAsk.requestId, ok: true });
    assert.deepEqual(plain((await stopReply).recordRequest), { action: 'stop', state: 'accepted' });
    assert.equal((await app.command({ type: 'command', requestId: 'app-stop-1', action: 'stop', tabId: 2, independent: true, reason: 'user', captureId: recording.captureId })).ok, true);
    const idle = await rpc(app, { type: 'status', tabId: 2 });
    assert.equal(idle.recording, false); assert.equal(idle.recordRequest, undefined);
    assert.equal(app.audio.at(-1).sent.find(packet => packet.type === 'stop').reason, 'user');
  } finally { app.close(); }
});

test('popup record: the app refusal reaches the popup; an accepted ask that never starts expires', async () => {
  let now = Date.now();
  class Clock extends Date { static now() { return now; } }
  const app = await setup(3, {}, [], null, 'token-', null, { Date: Clock });
  try {
    await optIn(app);
    let reply = rpc(app, { type: 'recordRequest', action: 'start', tabId: 2 });
    let ask = await asked(app);
    app.ports[0].onMessage.emit({ type: 'recordRequestResult', requestId: ask.requestId, ok: false, error: 'Choose what to record in Cassini first.', code: 'needsSetup' });
    reply = await reply;
    assert.equal(reply.error, 'Choose what to record in Cassini first.'); assert.equal(reply.code, 'needsSetup');
    assert.equal((await rpc(app, { type: 'status', tabId: 2 })).recordRequest, undefined);
    reply = rpc(app, { type: 'recordRequest', action: 'start', tabId: 2 });
    ask = await asked(app, 2);
    app.ports[0].onMessage.emit({ type: 'recordRequestResult', requestId: ask.requestId, ok: false, error: 'x'.repeat(3000), code: 'not a code' });
    reply = await reply;
    assert.equal(reply.error.length, 2000); assert.equal(reply.code, undefined);
    reply = rpc(app, { type: 'recordRequest', action: 'start', tabId: 2 });
    ask = await asked(app, 3);
    app.ports[0].onMessage.emit({ type: 'recordRequestResult', requestId: ask.requestId, ok: true });
    assert.equal((await reply).recordRequest.state, 'accepted');
    now += 9999;
    assert.equal((await rpc(app, { type: 'status', tabId: 2 })).recordRequest.state, 'accepted');
    now += 1;
    assert.equal((await rpc(app, { type: 'status', tabId: 2 })).recordRequest, undefined, 'an ask the app never acted on stops claiming progress');
    assert.equal(app.audio.length, 0);
  } finally { app.close(); }
});

test('popup record: Start is refused locally, before any ask, when the tab cannot be recorded', async () => {
  const app = await setup();
  try {
    await optIn(app);
    app.tabs.set(5, { id: 5, title: 'Settings', url: 'chrome://settings/' });
    const cases = [
      [{ action: 'start', tabId: 99 }, 'noSources'],
      [{ action: 'start', tabId: 5 }, 'notWebPage'],
      [{ action: 'start', tabId: 404 }, 'gone'],
      [{ action: 'stop', tabId: 2 }, 'gone'],
      [{ action: 'record', tabId: 2 }, 'invalid'],
      [{ action: 'start', tabId: -1 }, 'invalid'],
      [{ action: 'start', tabId: '2' }, 'invalid'],
    ];
    for (const [message, code] of cases) assert.equal((await rpc(app, { type: 'recordRequest', ...message })).code, code, JSON.stringify(message));
    assert.equal(sent(app.ports[0], 'recordRequest').length, 0);
    const other = await rpc(app, { type: 'recordRequest', action: 'start', tabId: 2 }, { id: 'extension', url: 'chrome-extension://extension/other.html' });
    assert.match(other.error, /Only the extension popup/);
    assert.equal(sent(app.ports[0], 'recordRequest').length, 0);
  } finally { app.close(); }
});

test('popup record: a disconnected or silent app fails the ask; it is never retried', async () => {
  const app = await setup();
  try {
    await optIn(app);
    let reply = rpc(app, { type: 'recordRequest', action: 'start', tabId: 2 });
    await asked(app);
    const timer = [...app.timers.entries()].find(([, value]) => value.ms === 10000);
    assert.ok(timer, 'the ask has a bounded answer deadline');
    app.timers.delete(timer[0]); timer[1].fn();
    reply = await reply;
    assert.equal(reply.code, 'timeout'); assert.match(reply.error, /did not answer in time/);
    assert.equal((await rpc(app, { type: 'status', tabId: 2 })).recordRequest, undefined);
    reply = rpc(app, { type: 'recordRequest', action: 'start', tabId: 2 });
    await asked(app, 2);
    app.ports[0].disconnect();
    reply = await reply;
    assert.equal(reply.code, 'appUnavailable');
    const status = await rpc(app, { type: 'status', tabId: 2 });
    assert.equal(status.appRecording, 'disconnected'); assert.equal(status.recordRequest, undefined);
    assert.equal((await rpc(app, { type: 'recordRequest', action: 'start', tabId: 2 })).code, 'appUnavailable');
    assert.equal(sent(app.ports[0], 'recordRequest').length, 2);
    assert.equal(app.audio.length, 0);
  } finally { app.close(); }
});

test('popup record: Start carries browser-owned meeting metadata even when the verified inventory omits the tab', async () => {
  const app = await setup();
  try {
    const control = app.api.appControl, port = control.port;
    port.onMessage.emit({ type: 'configure', capabilities: ['call-signals-v1', 'verified-auto-admission-v1'] });
    assert.equal(control.verifiedConfigured, true);
    // The fixture frame was repaired (no document-start observer): D80.3 keeps it out of the verified inventory.
    assert.deepEqual(plain((await app.api.meetings({ verified: true })).meetings), []);
    await optIn(app);
    const reply = rpc(app, { type: 'recordRequest', action: 'start', tabId: 2 });
    const ask = await asked(app);
    assert.equal(ask.title, 'Meeting B', 'the browser-owned title, never the page-reported one');
    assert.equal(ask.url, 'https://meet.example/call');
    assert.equal(ask.callGeneration, app.api.callInfo(2).generation);
    assert.equal(ask.pageState, 'present');
    assert.equal(ask.captureId, undefined);
    port.onMessage.emit({ type: 'recordRequestResult', requestId: ask.requestId, ok: true });
    assert.equal((await reply).error, undefined);
    // Stop asks carry no meeting metadata.
    const recording = await app.command({ type: 'command', requestId: 'app-manual-1', action: 'start', tabId: 2, mode: 'tracks', independent: true, callGeneration: ask.callGeneration });
    assert.equal(recording.ok, true);
    void rpc(app, { type: 'recordRequest', action: 'stop', tabId: 2 });
    const stopAsk = await asked(app, 2);
    assert.deepEqual(Object.keys(stopAsk).sort(), ['action', 'captureId', 'requestId', 'tabId', 'type']);
  } finally { app.close(); }
});

test('popup record: an ask never overtakes a pending inventory publication; a revocation meanwhile refuses it', async () => {
  const app = await setup();
  let release;
  try {
    const control = app.api.appControl, port = control.port;
    await optIn(app);
    const original = control.snapshot;
    control.snapshot = async options => { await new Promise(resolve => { release = resolve; }); return original(options); };
    control.publish();
    await until(() => release);
    let entered = waiting(control);
    const reply = rpc(app, { type: 'recordRequest', action: 'start', tabId: 2 });
    await entered;
    assert.equal(sent(port, 'recordRequest').length, 0, 'the ask waits for the in-flight snapshot');
    const before = port.sent.length;
    control.snapshot = original;
    release();
    const ask = await asked(app);
    const meetings = port.sent.findIndex((m, index) => index >= before && m.type === 'meetings');
    assert.ok(meetings >= 0 && meetings < port.sent.indexOf(ask), 'meetings is published before the ask');
    port.onMessage.emit({ type: 'recordRequestResult', requestId: ask.requestId, ok: false, error: 'No.', code: 'needsSetup' });
    await reply;
    // The app revokes while the publication is still draining: nothing is sent.
    release = null;
    control.snapshot = async options => { await new Promise(resolve => { release = resolve; }); return original(options); };
    control.publish();
    await until(() => release);
    entered = waiting(control);
    const revoked = rpc(app, { type: 'recordRequest', action: 'start', tabId: 2 });
    await entered;
    port.onMessage.emit({ type: 'recordRequests', enabled: false });
    control.snapshot = original;
    release();
    assert.equal((await revoked).code, 'appUnavailable');
    assert.equal(sent(port, 'recordRequest').length, 1);
  } finally { release?.(); app.close(); }
});

test('popup record: a held publication cannot outlive the ask deadline or its connection', async () => {
  const app = await setup();
  let release;
  try {
    const control = app.api.appControl, port = control.port;
    await optIn(app);
    const original = control.snapshot;
    // A snapshot that never settles until the end of the test.
    control.snapshot = async options => { await new Promise(resolve => { release = resolve; }); return original(options); };
    control.publish();
    await until(() => release);
    let entered = waiting(control);
    const timedOut = rpc(app, { type: 'recordRequest', action: 'start', tabId: 2 });
    const requestId = await entered;
    assert.deepEqual(plain((await rpc(app, { type: 'status', tabId: 2 })).recordRequest), { action: 'start', state: 'pending' });
    // The deadline was armed before the wait: firing it refuses the ask with the snapshot still held.
    const [timerId, timer] = [...app.timers].find(([, value]) => value.ms === 10000);
    app.timers.delete(timerId); timer.fn();
    assert.equal((await timedOut).code, 'timeout');
    assert.equal((await rpc(app, { type: 'status', tabId: 2 })).recordRequest, undefined, 'no stale pending state');
    assert.equal(control.recordRequests.has(requestId), false);
    // A retry is not refused as busy; it waits again, and a disconnect ends it with the snapshot still held.
    entered = waiting(control);
    const lost = rpc(app, { type: 'recordRequest', action: 'start', tabId: 2 });
    await entered;
    port.disconnect();
    assert.equal((await lost).code, 'appUnavailable');
    assert.equal((await rpc(app, { type: 'status', tabId: 2 })).recordRequest, undefined);
    // Releasing the stalled publication afterwards never sends either ask.
    const held = release; release = null; held();
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(sent(port, 'recordRequest').length, 0);
  } finally { release?.(); app.close(); }
});

test('popup record: a call change during publication refuses Start instead of mixing old metadata with the new call', async () => {
  const app = await setup();
  let release;
  try {
    const control = app.api.appControl, port = control.port;
    await optIn(app);
    const original = control.snapshot;
    control.snapshot = async options => { await new Promise(resolve => { release = resolve; }); return original(options); };
    control.publish();
    await until(() => release);
    const before = app.api.callInfo(2).generation;
    const entered = waiting(control);
    const reply = rpc(app, { type: 'recordRequest', action: 'start', tabId: 2 });
    await entered;
    // The tab navigates to another call while the ask waits; its sources come back.
    app.tabs.set(2, { id: 2, title: 'Other meeting', url: 'https://meet.example/other' });
    app.api.callChanged(2, 'https://meet.example/other');
    app.pages[0].onMessage.emit({ type: 'discovery', sourceCount: 3 });
    assert.notEqual(app.api.callInfo(2).generation, before);
    control.snapshot = original;
    const held = release; release = null; held();
    // Whichever comes first: a refusal (correct) or an ask naming a capture (the regression).
    const answer = await Promise.race([reply, asked(app).then(ask => ({ leaked: ask }))]);
    assert.equal(answer.leaked, undefined, 'the held Stop must not become an ask for the replacement');
    assert.equal(answer.code, 'gone');
    assert.match(answer.error, /meeting changed/);
    assert.equal(sent(port, 'recordRequest').length, 0, 'no ask pairs the new call with the old title and URL');
    assert.equal((await rpc(app, { type: 'status', tabId: 2 })).recordRequest, undefined);
  } finally { release?.(); app.close(); }
});

test('popup record: a Stop held behind publication stays bound to its capture and never stops a replacement', async () => {
  const app = await setup();
  let release;
  try {
    const control = app.api.appControl, port = control.port;
    await optIn(app);
    const generation = app.api.callInfo(2).generation;
    const startA = { type: 'command', requestId: 'app-a', action: 'start', tabId: 2, mode: 'tracks', independent: true, callGeneration: generation };
    assert.equal((await app.command(startA)).ok, true);
    const captureA = (await rpc(app, { type: 'status', tabId: 2 })).captureId;
    const original = control.snapshot;
    control.snapshot = async options => { await new Promise(resolve => { release = resolve; }); return original(options); };
    control.publish();
    await until(() => release);
    const entered = waiting(control);
    const reply = rpc(app, { type: 'recordRequest', action: 'stop', tabId: 2 });
    await entered;
    // While the ask waits, the app stops A and starts B in the same tab.
    assert.equal((await app.command({ type: 'command', requestId: 'app-stop-a', action: 'stop', tabId: 2, independent: true, reason: 'user', captureId: captureA })).ok, true);
    assert.equal((await app.command({ ...startA, requestId: 'app-b' })).ok, true);
    const captureB = (await rpc(app, { type: 'status', tabId: 2 })).captureId;
    assert.notEqual(captureB, captureA);
    control.snapshot = original;
    const held = release; release = null; held();
    // Whichever comes first: a refusal (correct) or an ask naming a capture (the regression).
    const answer = await Promise.race([reply, asked(app).then(ask => ({ leaked: ask }))]);
    assert.equal(answer.leaked, undefined, 'the held Stop must not become an ask for the replacement');
    assert.equal(answer.code, 'gone');
    assert.equal(sent(port, 'recordRequest').length, 0, 'no Stop ask ever names the replacement capture');
    const status = await rpc(app, { type: 'status', tabId: 2 });
    assert.equal(status.recording, true); assert.equal(status.captureId, captureB);
    assert.equal(status.recordRequest, undefined);
    // An unreplaced capture still gets its Stop ask after the publication drains.
    control.snapshot = async options => { await new Promise(resolve => { release = resolve; }); return original(options); };
    control.publish();
    await until(() => release);
    const again = waiting(control);
    const stopB = rpc(app, { type: 'recordRequest', action: 'stop', tabId: 2 });
    await again;
    control.snapshot = original;
    const held2 = release; release = null; held2();
    const ask = await asked(app);
    assert.equal(ask.captureId, captureB);
    port.onMessage.emit({ type: 'recordRequestResult', requestId: ask.requestId, ok: true });
    assert.equal((await stopB).recordRequest.state, 'accepted');
  } finally { release?.(); app.close(); }
});

test('popup record: an accepted ask ends with its connection, opt-in, or the app command it was waiting for', async () => {
  const app = await setup();
  const accept = async (count, action = 'start') => {
    const reply = rpc(app, { type: 'recordRequest', action, tabId: 2 });
    const ask = await asked(app, count);
    app.ports.at(-1).onMessage.emit({ type: 'recordRequestResult', requestId: ask.requestId, ok: true });
    assert.deepEqual(plain((await reply).recordRequest), { action, state: 'accepted' });
    return ask;
  };
  try {
    await optIn(app);
    // Revocation clears it.
    await accept(1);
    app.ports[0].onMessage.emit({ type: 'recordRequests', enabled: false });
    assert.equal((await rpc(app, { type: 'status', tabId: 2 })).recordRequest, undefined);
    // Disconnect clears it; after reconnect and opt-in the user can decide again at once.
    await optIn(app);
    await accept(2);
    const next = await app.reconnect();
    let status = await rpc(app, { type: 'status', tabId: 2 });
    assert.equal(status.recordRequest, undefined); assert.equal(status.appRecording, 'unsupported');
    await optIn(app);
    status = await rpc(app, { type: 'status', tabId: 2 });
    assert.equal(status.recordRequest, undefined); assert.equal(status.appRecording, 'available');
    // The app's Start command fails after acceptance: the wait ends.
    let ask = await accept(1);
    let result = await app.command({ type: 'command', requestId: 'app-1', action: 'start', tabId: 2, mode: 'tracks', independent: true, callGeneration: 'stale-call' });
    assert.equal(result.ok, false);
    assert.equal((await rpc(app, { type: 'status', tabId: 2 })).recordRequest, undefined);
    // The app's Start command fails before its acceptance arrives: no accepted wait is created.
    const reply = rpc(app, { type: 'recordRequest', action: 'start', tabId: 2 });
    ask = await asked(app, 2);
    result = await app.command({ type: 'command', requestId: 'app-2', action: 'start', tabId: 2, mode: 'tracks', independent: true, callGeneration: 'stale-call' });
    assert.equal(result.ok, false);
    next.onMessage.emit({ type: 'recordRequestResult', requestId: ask.requestId, ok: true });
    assert.equal((await reply).recordRequest, undefined);
    assert.equal((await rpc(app, { type: 'status', tabId: 2 })).recordRequest, undefined);
    assert.equal(app.audio.length, 0);
  } finally { app.close(); }
});
