// D58 (R79): ok:false control results carry an optional, open `code`.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { setup, event, until } = require('./support/app-control-harness.cjs');

const KNOWN = ['busy', 'limit', 'noSources', 'gone', 'notWebPage', 'pageUnreachable', 'invalid', 'resumeRefused', 'workletBlocked', 'audioBlocked', 'unsupported'];
const BUSY = 'A recording is already running. Stop it before starting another.';
const start = (requestId, extra = {}) => ({ type: 'command', requestId, action: 'start', tabId: 2, mode: 'tracks', ...extra });

async function refusal(app, command, code, error) {
  const result = await app.command(command);
  assert.equal(result.ok, false);
  if (error !== undefined) assert.equal(result.error, error);
  if (code === undefined) assert.equal('code' in result, false, `unexpected code ${result.code}`);
  else assert.equal(result.code, code);
  return result;
}

test('result codes: every code the extension sets is a known pattern-valid value', () => {
  const extension = path.resolve(__dirname, '..');
  const sources = ['background.js', 'app-control.js', 'capture.js'].map(name => fs.readFileSync(path.join(extension, name), 'utf8')).join('\n');
  const found = new Set();
  for (const pattern of [/refused\("[^"]*", "(\w+)"\)/g, /classified\("(\w+)"\)/g, /resultCode: ["'](\w+)["']/g, /ok: false, error: '[^']*', code: '(\w+)'/g])
    for (const match of sources.matchAll(pattern)) found.add(match[1]);
  assert.ok(found.size >= 9, [...found].join());
  for (const code of found) {
    assert.ok(KNOWN.includes(code), code);
    assert.match(code, /^[A-Za-z]{1,32}$/);
  }
});

test('result codes: malformed commands are invalid', async () => {
  const app = await setup();
  try {
    await refusal(app, { type: 'command', requestId: 'action', action: 'pause', tabId: 2 }, 'invalid', 'Choose a meeting to record.');
    await refusal(app, { type: 'command', requestId: 'tab', action: 'start', tabId: -1, mode: 'tracks' }, 'invalid', 'Choose a meeting to record.');
    await refusal(app, start('whole-tab', { mode: 'tab' }), 'invalid', 'To record the whole tab, click Record the whole tab in the browser.');
    await refusal(app, start('option', { captureLocal: 'yes' }), 'invalid', 'Choose a meeting to record.');
    await refusal(app, start('stop-only-option', { reason: 'user' }), 'invalid', 'Choose a meeting to record.');
    assert.equal(app.audio.length, 0);
  } finally { app.close(); }
});

test('result codes: a second recording is busy, for start and for a legacy stop of another tab', async () => {
  const app = await setup();
  try {
    assert.equal((await app.command(start('record'))).ok, true);
    await refusal(app, start('again'), 'busy', BUSY);
    await refusal(app, { type: 'command', requestId: 'other-stop', action: 'stop', tabId: 99 }, 'busy', BUSY);
    await refusal(app, start('same-tab', { independent: true }), 'busy', BUSY);
  } finally { app.close(); }
});

test('result codes: a meeting without discovered audio is noSources', async () => {
  const app = await setup(0);
  try {
    await refusal(app, start('quiet'), 'noSources', 'No audio is available from that meeting yet.');
    assert.equal(app.audio.length, 0);
  } finally { app.close(); }
});

test('result codes: a missing tab, a changed call and an inactive capture are gone', async () => {
  const app = await setup();
  try {
    app.frame(7, 2);
    await refusal(app, start('closed', { tabId: 7 }), 'gone', 'That meeting is no longer available.');
    await refusal(app, start('stale-call', { independent: true, callGeneration: 'an-older-call' }), 'gone', 'That call is no longer active.');
    await refusal(app, { type: 'command', requestId: 'stale-stop', action: 'stop', tabId: 2, independent: true, captureId: 'not-this-capture' },
      'gone', 'That recording is no longer active.');
    assert.equal(app.audio.length, 0);
  } finally { app.close(); }
});

test('result codes: a non-web tab is notWebPage', async () => {
  const app = await setup();
  try {
    app.tabs.set(5, { id: 5, title: 'Settings', url: 'chrome://settings/' });
    app.frame(5, 2);
    await refusal(app, start('settings', { tabId: 5 }), 'notWebPage', 'Open the meeting tab first. Recording works on web pages only.');
  } finally { app.close(); }
});

test('result codes: a page whose frames are gone by Start is pageUnreachable', async () => {
  const app = await setup();
  try {
    const lookup = app.chrome.tabs.get;
    let once = true; // Only the Start lookup: a snapshot's lookup must not disconnect again.
    app.chrome.tabs.get = async id => { if (once) { once = false; app.pages[0].onDisconnect.emit(); } return lookup(id); };
    await refusal(app, start('unreachable'), 'pageUnreachable', 'Cassini cannot reach this page yet. Reload the meeting tab, then try again.');
    assert.equal(app.audio.length, 0);
  } finally { app.close(); }
});

test('result codes: a full command table is limit', async () => {
  const ports = [];
  const runtime = { connectNative() { const port = { sent: [], onMessage: event(), onDisconnect: event(), postMessage(m) { this.sent.push(m); }, disconnect() {} }; ports.push(port); return port; } };
  const alarms = { onAlarm: event(), async create() {}, async clear() {} };
  const context = vm.createContext({ setTimeout, clearTimeout });
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../app-control.js'), 'utf8').replace('export class AppControl', 'class AppControl') + '\nglobalThis.Control=AppControl;', context);
  const control = new context.Control({ runtime, alarms, snapshot: async () => [], command: () => new Promise(() => {}), later: () => 0, cancel: () => {} });
  try {
    control.start();
    for (let index = 0; index < 128; index++) ports[0].onMessage.emit(start('pending-' + index));
    ports[0].onMessage.emit(start('one-too-many'));
    const result = ports[0].sent.find(m => m.type === 'result' && m.requestId === 'one-too-many');
    assert.deepEqual({ ...result }, { type: 'result', requestId: 'one-too-many', ok: false,
      error: 'Cassini has received too many commands. Reconnect and try again.', code: 'limit' });
  } finally { control.close(); }
});

test('result codes: an unconfirmed resume is resumeRefused; a host error keeps its audio-plane code out of the result', async () => {
  for (const reply of ['unconfirmed', 'host-error']) {
    const app = await setup(3, {}, [], () => ({ sent: [], closed: false, onMessage: event(), onDisconnect: event(),
      disconnect() { this.closed = true; },
      postMessage(packet) {
        this.sent.push(packet);
        queueMicrotask(() => this.onMessage.emit(
          packet.type === 'resume' && reply === 'host-error' ? { type: 'error', code: 'state', scope: 'command', message: 'Target is no longer active.' }
          : packet.type === 'resume' ? { type: 'started', path: '/test/stand-in', features: ['source-outcomes-v1'] }
          : packet.type === 'stop' ? { type: 'stopped', path: '/test/stand-in' } : { type: 'ack' }));
      } }));
    try {
      const command = start('resume', { independent: true, callGeneration: app.api.callInfo(2).generation, captureLocal: false, microphone: false,
        resumeCapture: { captureId: 'stand-in', timelineOriginMs: 1000 } });
      if (reply === 'unconfirmed') await refusal(app, command, 'resumeRefused', 'That recording could not be resumed.');
      else await refusal(app, command, undefined, 'Target is no longer active.');
    } finally { app.close(); }
  }
});

test('result codes: a Start the app cancelled carries no code', async () => {
  const app = await setup(); let release;
  try {
    const lookup = app.chrome.tabs.get; let entered = false;
    const gate = new Promise(resolve => { release = resolve; });
    app.chrome.tabs.get = async id => { entered = true; await gate; return lookup(id); };
    const pending = app.command(start('cancelled', { independent: true }));
    await until(() => entered);
    const stopped = app.command({ type: 'command', requestId: 'stop', action: 'stop', tabId: 2, independent: true });
    release();
    const result = await pending;
    assert.equal(result.ok, false); assert.equal(result.error, 'Recording could not start.'); assert.equal('code' in result, false);
    assert.equal((await stopped).ok, true);
  } finally { release?.(); app.close(); }
});

const captureSource = fs.readFileSync(path.join(__dirname, '../capture.js'), 'utf8');
async function blocked(configure) {
  const saved = new Map(['AudioContext', 'AudioWorkletNode', 'Audio'].map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)]));
  class Graph { connect(value) { return value; } disconnect() {} }
  class Context {
    sampleRate = 48000; state = 'running'; currentTime = 0; destination = new Graph(); audioWorklet = { addModule: async () => {} };
    createMediaStreamSource() { return new Graph(); } createGain() { return Object.assign(new Graph(), { gain: { value: 0 } }); }
    createConstantSource() { return Object.assign(new Graph(), { offset: { value: 0 }, start() {} }); }
    addEventListener() {} removeEventListener() {} async resume() {} async close() { this.state = 'closed'; }
  }
  class Worklet extends Graph { constructor() { super(); this.port = { postMessage() {} }; } connect(target) { queueMicrotask(() => this.port.onmessage?.({ data: { ready: true } })); return target; } }
  globalThis.AudioContext = Context; globalThis.AudioWorkletNode = Worklet;
  globalThis.Audio = class { async play() {} pause() {} };
  try {
    const { capture } = await import('data:text/javascript;base64,' + Buffer.from(captureSource).toString('base64'));
    const options = { stream: {}, id: 'one', label: 'Guest', kind: 'remote', origin: 0, workletURL: '', send: async () => {}, live: () => false };
    configure({ Context, options });
    const recorder = await capture(options);
    assert.equal(recorder.failed, true);
    return await recorder.stop().then(() => assert.fail('a refused source must fail'), error => error);
  } finally { for (const [key, descriptor] of saved) { if (descriptor) Object.defineProperty(globalThis, key, descriptor); else delete globalThis[key]; } }
}

test('result codes: capture tags a blocked worklet and blocked audio at the point of failure', async () => {
  let error = await blocked(({ Context }) => { const create = Context; globalThis.AudioContext = class extends create { constructor() { super(); this.audioWorklet = { addModule: async () => { throw new Error('generated worklet refusal'); } }; } }; });
  assert.equal(error.resultCode, 'workletBlocked'); assert.equal(error.message, 'generated worklet refusal');
  error = await blocked(() => { globalThis.Audio = class { async play() { throw new Error('generated autoplay refusal'); } pause() {} }; });
  assert.equal(error.resultCode, 'audioBlocked'); assert.equal(error.message, 'generated autoplay refusal');
  error = await blocked(({ Context }) => { const create = Context; globalThis.AudioContext = class extends create { constructor() { super(); this.state = 'suspended'; } }; });
  assert.equal(error.resultCode, 'audioBlocked'); assert.match(error.message, /^Audio capture is suspended/);
  error = await blocked(({ Context }) => { const create = Context; globalThis.AudioContext = class extends create { async resume() { throw new Error('generated resume refusal'); } }; });
  assert.equal(error.resultCode, 'audioBlocked');
  assert.equal(error.code, undefined, 'resultCode never replaces the audio-plane code');
});

for (const abort of ['callChanged', 'tabClosed', 'stop']) test('result codes: a Start aborted in flight by ' + abort + (abort === 'stop' ? ' carries no code' : ' is gone'), async () => {
  const app = await setup(); let release;
  try {
    const gate = new Promise(resolve => { release = resolve; }); let entered = false;
    app.chrome.tabs.sendMessage = async () => { entered = true; await gate; return {}; };
    const pending = app.command(start('in-flight', { independent: true, callGeneration: app.api.callInfo(2).generation }));
    await until(() => entered);
    let stopped;
    if (abort === 'callChanged') app.chrome.tabs.onUpdated.emit(2, { url: 'https://meet.example/another-call' });
    else if (abort === 'tabClosed') { app.tabs.delete(2); app.chrome.tabs.onRemoved.emit(2); }
    else stopped = app.command({ type: 'command', requestId: 'stop', action: 'stop', tabId: 2, independent: true });
    release();
    const expected = { callChanged: ['That call is no longer active.', 'gone'], tabClosed: ['That meeting is no longer available.', 'gone'],
      stop: ['Recording could not start.', undefined] }[abort];
    const result = await pending;
    assert.equal(result.ok, false); assert.equal(result.error, expected[0]);
    if (expected[1] === undefined) assert.equal('code' in result, false); else assert.equal(result.code, expected[1]);
    assert.equal(app.audio.length, 0, 'no native folder was requested');
    if (stopped) assert.equal((await stopped).ok, true);
  } finally { release?.(); app.close(); }
});

test('result codes: losing the native host during Start carries no code', async () => {
  const app = await setup(3, {}, [], () => ({ sent: [], closed: false, onMessage: event(), onDisconnect: event(),
    disconnect() { this.closed = true; },
    postMessage(packet) {
      if (this.gone) throw new Error('Attempting to use a disconnected port object.');
      this.sent.push(packet);
      if (packet.type === 'start') { this.gone = true; queueMicrotask(() => this.onDisconnect.emit()); }
    } }));
  try {
    const result = await app.command(start('host-lost', { independent: true }));
    assert.equal(result.ok, false); assert.equal('code' in result, false, `unexpected code ${result.code}`);
  } finally { app.close(); }
});

// Codex review of #35: an active same-tab recording must answer busy, never noSources,
// or an app that falls back on noSources records the meeting twice (D58 #11).
test('result codes: a Start for a recording tab whose discovery dropped to zero is busy, not noSources', async () => {
  const app = await setup();
  try {
    assert.equal((await app.command(start('record', { independent: true }))).ok, true);
    app.pages[0].onMessage.emit({ type: 'discovery', sourceCount: 0 });
    assert.equal((await app.api.meetings()).meetings[0].sourceCount, 0);
    await refusal(app, start('again', { independent: true }), 'busy', BUSY);
    await refusal(app, start('legacy-again'), 'busy', BUSY);
    await refusal(app, start('resume-again', { independent: true, callGeneration: app.api.callInfo(2).generation,
      resumeCapture: { captureId: 'stand-in', timelineOriginMs: 1000 } }), 'busy', BUSY);
    await refusal(app, start('bad-options', { independent: true, captureLocal: 'yes' }), 'busy', BUSY);
    assert.equal(app.api.controlStatus(2).recording, true, 'the first recording is still active');
    assert.equal(app.audio.length, 1);
  } finally { app.close(); }
});

test('result codes: a full command table still answers busy for a busy tab', async () => {
  const ports = [];
  const runtime = { connectNative() { const port = { sent: [], onMessage: event(), onDisconnect: event(), postMessage(m) { this.sent.push(m); }, disconnect() {} }; ports.push(port); return port; } };
  const alarms = { onAlarm: event(), async create() {}, async clear() {} };
  const context = vm.createContext({ setTimeout, clearTimeout });
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../app-control.js'), 'utf8').replace('export class AppControl', 'class AppControl') + '\nglobalThis.Control=AppControl;', context);
  const control = new context.Control({ runtime, alarms, snapshot: async () => [], command: () => new Promise(() => {}), later: () => 0, cancel: () => {},
    busy: message => message.action === 'start' && message.tabId === 2 });
  try {
    control.start();
    for (let index = 0; index < 128; index++) ports[0].onMessage.emit(start('pending-' + index, { tabId: 3 }));
    ports[0].onMessage.emit(start('busy-tab'));
    ports[0].onMessage.emit(start('other-tab', { tabId: 4 }));
    const result = id => ({ ...ports[0].sent.find(m => m.type === 'result' && m.requestId === id) });
    assert.equal(result('busy-tab').code, 'busy'); assert.equal(result('busy-tab').error, BUSY);
    assert.equal(result('other-tab').code, 'limit');
  } finally { control.close(); }
});
