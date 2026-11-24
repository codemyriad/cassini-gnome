// PR #108 review findings: what the popup offers is what the worker accepts, its actions are bound to what it
// displayed, and only the open check (or Check again) touches the meeting page. VM fixtures only.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { setup, event, until } = require('./support/app-control-harness.cjs');

const popup = app => ({ id: 'extension', url: app.chrome.runtime.getURL('popup.html') });
const rpc = (app, message) => new Promise(resolve => app.chrome.runtime.onMessage.emit({ target: 'background', ...message }, popup(app), resolve));
const plain = value => JSON.parse(JSON.stringify(value));
function relayPort(app, tabId, { frameId = 0, documentId, url, trusted = true, ready = true } = {}) {
  const port = { name: 'cassini-frame', sender: { id: 'extension', tab: { id: tabId }, frameId, documentId, url: url || app.tabs.get(tabId)?.url },
    onMessage: event(), onDisconnect: event(), sent: [], postMessage(m) { this.sent.push(m); } };
  app.chrome.runtime.onConnect.emit(port);
  const hello = port.sent.find(m => m.type === 'relayHello');
  port.onMessage.emit({ ...hello, observerDocumentStart: trusted });
  port.onMessage.emit({ ...hello, type: ready ? 'relayReady' : 'relayUnavailable', observerDocumentStart: trusted });
  return port;
}
async function optIn(app) {
  const port = app.ports.at(-1);
  port.onMessage.emit({ type: 'recordRequests', enabled: true });
  await until(() => port.sent.some(m => m.type === 'recordRequestsConfigured'));
}

test('the popup offers a reload for a hookless already-open tab and the worker accepts that same reload', async () => {
  const { popupState } = await import('../popup-state.mjs');
  const app = await setup(0), reloads = [];
  try {
    app.chrome.tabs.reload = async id => reloads.push(id);
    app.tabs.set(5, { id: 5, title: 'Talk', url: 'https://talk.example/call/abc' });
    relayPort(app, 5, { documentId: 'doc-5', trusted: false, ready: false });
    const status = await rpc(app, { type: 'status', tabId: 5, fresh: true });
    const view = popupState(status, app.tabs.get(5));
    assert.equal(view.reloadTabId, 5, 'the popup offers the reload');
    const reply = await rpc(app, { type: 'reloadMeetingTab', tabId: 5 });
    assert.equal(reply.error, undefined, JSON.stringify(reply));
    assert.deepEqual(reloads, [5]);
  } finally { app.close(); }
});

test('a repaired relay on a page with no meeting sources is neither offered nor reloaded', async () => {
  const { popupState } = await import('../popup-state.mjs');
  const app = await setup(0), reloads = [];
  try {
    app.chrome.tabs.reload = async id => reloads.push(id);
    app.tabs.set(5, { id: 5, title: 'Wiki', url: 'https://wiki.example/page' });
    relayPort(app, 5, { documentId: 'doc-5', trusted: false });
    const status = await rpc(app, { type: 'status', tabId: 5, fresh: true });
    assert.equal(status.tabEvidence.transport.documentStart, false);
    assert.equal(status.browserHealth.tabs.some(t => t.tabId === 5), false, 'no sources, no meeting row');
    assert.equal(popupState(status, app.tabs.get(5)).reloadTabId, null);
    assert.equal((await rpc(app, { type: 'reloadMeetingTab', tabId: 5 })).code, 'gone');
    assert.deepEqual(reloads, []);
  } finally { app.close(); }
});

test('no reload while the app is answering a Start ask, in the card or in the worker', async () => {
  const { popupState } = await import('../popup-state.mjs');
  const app = await setup(1), reloads = [];
  try {
    app.chrome.tabs.reload = async id => reloads.push(id);
    await optIn(app);
    const reply = rpc(app, { type: 'recordRequest', action: 'start', tabId: 2 });
    await until(() => app.ports.at(-1).sent.some(m => m.type === 'recordRequest'));
    const ask = app.ports.at(-1).sent.find(m => m.type === 'recordRequest');
    app.ports.at(-1).onMessage.emit({ type: 'recordRequestResult', requestId: ask.requestId, ok: true });
    assert.equal((await reply).recordRequest.state, 'accepted');
    const status = await rpc(app, { type: 'status', tabId: 2 });
    assert.equal(popupState(status, app.tabs.get(2)).reloadTabId, null, 'the waiting card offers no reload');
    assert.equal((await rpc(app, { type: 'reloadMeetingTab', tabId: 2 })).code, 'busy');
    assert.deepEqual(reloads, []);
  } finally { app.close(); }
});

test('whole-tab Start names the displayed tab, never the active one', async () => {
  const app = await setup(0), requested = [];
  try {
    app.chrome.tabCapture.getMediaStreamId = async options => { requested.push(options.targetTabId); throw Error('stop here: no gesture in the fixture'); };
    const reply = await rpc(app, { type: 'start', mode: 'tab', tabId: 2 });
    assert.match(reply.error, /gesture/);
    assert.deepEqual(requested, [2], 'tab 2 was rendered, tab 99 is the active tab');
  } finally { app.close(); }
});

test('a popup Stop names its tab and capture and never stops a different or newer recording', async () => {
  const app = await setup(1);
  try {
    const first = await app.command({ type: 'command', requestId: 's1', action: 'start', mode: 'tracks', tabId: 2, independent: true });
    assert.equal(first.ok, true);
    const firstId = (await rpc(app, { type: 'status', tabId: 2 })).captureId;
    assert.equal(typeof firstId, 'string');
    await app.command({ type: 'command', requestId: 'x1', action: 'stop', tabId: 2, independent: true });
    app.frame(99, 1);
    assert.equal((await app.command({ type: 'command', requestId: 's2', action: 'start', mode: 'tracks', tabId: 99, independent: true })).ok, true);
    const secondId = (await rpc(app, { type: 'status', tabId: 99 })).captureId;
    // A stale Stop click for tab 2's earlier capture must not stop tab 99's recording.
    assert.equal((await rpc(app, { type: 'stop', tabId: 2, captureId: firstId })).code, 'gone');
    assert.equal((await rpc(app, { type: 'stop', tabId: 99, captureId: firstId })).code, 'gone', 'a different capture id is refused');
    assert.equal(app.api.status().recording, true);
    assert.equal((await rpc(app, { type: 'stop', tabId: 99, captureId: secondId })).error, undefined);
  } finally { while (app.api.status().recording) await app.api.stop(); app.close(); }
});

test('change pushes never run scripts in the meeting tab; they reuse the open check for the same document only', async () => {
  const app = await setup(0);
  try {
    app.tabs.set(5, { id: 5, title: 'Meet', url: 'https://meet.example/call' });
    let reads = 0;
    app.chrome.scripting = { async executeScript(request) {
      if (request.world === 'MAIN') {
        reads++;
        return [{ frameId: 0, documentId: request.target.documentIds[0], result: { version: 2, cohortQualified: true, documentGeneration: 'hook-' + request.target.documentIds[0],
          hookGeneration: '0', revision: reads + 1, ageMs: 10, receivedPeers: 1, capturedSendingPeers: 1, samePeerCount: 1, receivedVersion: reads, sentVersion: reads,
          ended: false, sourceCount: 1, heard: true } }];
      }
      return [{ documentId: request.target.documentIds[0], frameId: 0, result: [] }];
    } };
    const port = relayPort(app, 5, { documentId: 'doc-5' });
    port.onMessage.emit({ type: 'discovery', sourceCount: 1, heard: true, documentGeneration: 'hook-doc-5', callGeneration: '0' });
    await until(() => [...app.api.frames].some(f => f.tabId === 5 && f.hookReady));
    const opened = await rpc(app, { type: 'status', tabId: 5, fresh: true });
    const afterOpen = reads;
    assert.ok(afterOpen >= 1, 'the open check reads the collector');
    for (let i = 0; i < 3; i++) {
      const push = await rpc(app, { type: 'status', tabId: 5 });
      assert.equal(push.tabEvidence.callEvidence.state, opened.tabEvidence.callEvidence.state, 'the last reading is reused');
    }
    assert.equal(reads, afterOpen, 'plain refreshes run no script in the meeting tab');
    // The document is replaced: the old reading is never shown for the new one.
    port.onDisconnect.emit();
    relayPort(app, 5, { documentId: 'doc-5b' });
    await new Promise(resolve => setTimeout(resolve, 50)); // the new relay's own registration may read the collector
    const beforeStatus = reads;
    const replaced = await rpc(app, { type: 'status', tabId: 5 });
    assert.equal(replaced.tabEvidence.callEvidence.state, 'unknown', 'the old reading is not shown for the new document');
    assert.equal(reads, beforeStatus, 'the status call itself ran no script');
  } finally { app.close(); }
});

test('on open, a missing main relay is reconnected through frame 0 even when a child relay answers', async () => {
  const app = await setup(0), messages = [], injections = [];
  try {
    app.tabs.set(5, { id: 5, title: 'Meet', url: 'https://meet.example/call' });
    relayPort(app, 5, { frameId: 4, documentId: 'doc-child', url: 'https://meet.example/frame' });
    const instance = app.api.controlStatus().browserHealth.extensionInstance;
    app.chrome.tabs.sendMessage = async (tabId, message, options = {}) => { messages.push(options); return options.frameId === undefined ? { connected: true, protocol: 1, instanceId: instance } : null; };
    app.chrome.scripting = { async executeScript(request) { injections.push(request.target); return [{ documentId: 'x', frameId: 0, result: null }]; } };
    const status = await rpc(app, { type: 'status', tabId: 5, fresh: true });
    assert.ok(messages.length >= 1 && messages.every(m => m.frameId === 0), 'the probe targets frame 0: ' + JSON.stringify(messages));
    assert.ok(injections.some(t => JSON.stringify(t.frameIds) === '[0]'), 'a missing main relay is injected: ' + JSON.stringify(injections));
    assert.notEqual(status.tabEvidence.transport.detail.repair, null);
  } finally { app.close(); }
});

test('opening the popup on a tab that cannot be injected returns at once instead of waiting out the handshake', async () => {
  const app = await setup(0), waits = [];
  try {
    const later = app.api.relayTimers.later;
    app.api.relayTimers.later = (fn, ms) => { waits.push(ms); return later(fn, ms); };
    app.tabs.set(9, { id: 9, title: 'Settings', url: 'chrome://settings' });
    app.chrome.tabs.sendMessage = async () => { throw Error('No receiver'); };
    app.chrome.scripting = { async executeScript() { throw Error('Cannot access a chrome:// URL'); } };
    const started = Date.now();
    const status = await rpc(app, { type: 'status', tabId: 9, fresh: true });
    assert.equal(waits.filter(ms => ms === 100).length, 0, 'no 100 ms polling loop');
    assert.ok(Date.now() - started < 1500);
    assert.equal(status.tabEvidence.transport.detail.main, false);
  } finally { app.close(); }
});

test('whole-tab Start is reported as unavailable while another recording runs', async () => {
  const { popupState } = await import('../popup-state.mjs');
  const app = await setup(1);
  try {
    app.tabs.set(3, { id: 3, title: 'Other', url: 'https://other.example/call' });
    assert.equal((await app.command({ type: 'command', requestId: 's1', action: 'start', mode: 'tracks', tabId: 2, independent: true })).ok, true);
    const status = await rpc(app, { type: 'status', tabId: 3 });
    assert.equal(status.otherCapture, true);
    const view = popupState(status, app.tabs.get(3));
    assert.equal(view.fallback.enabled, false);
    assert.match(view.fallback.hint, /Another recording is running/);
    assert.equal((await rpc(app, { type: 'status', tabId: 2 })).otherCapture, false, 'the recording tab itself is not "another"');
  } finally { while (app.api.status().recording) await app.api.stop(); app.close(); }
});

test('open popups hear when the app connects, disconnects or changes the record opt-in', async () => {
  const app = await setup(0);
  try {
    const popupPort = { name: 'cassini-popup', sender: { id: 'extension', url: app.chrome.runtime.getURL('popup.html') }, onMessage: event(), onDisconnect: event(), sent: [], postMessage(m) { this.sent.push(m); } };
    app.chrome.runtime.onConnect.emit(popupPort);
    const before = popupPort.sent.length;
    await optIn(app);
    assert.ok(popupPort.sent.length > before, 'the record opt-in reaches the popup');
    const afterOptIn = popupPort.sent.length;
    app.ports.at(-1).onDisconnect.emit();
    assert.ok(popupPort.sent.length > afterOptIn, 'losing the app link reaches the popup');
  } finally { app.close(); }
});
