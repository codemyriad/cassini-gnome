const { test } = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const fs = require('node:fs');
const { runCase, setup, until } = require('./support/app-control-harness.cjs');
const root = path.resolve(__dirname, '../..');
const contract = fs.existsSync(path.join(root, 'conformance/control/vectors.json')) ? root : path.join(root, 'browser-protocol');
const vectors = JSON.parse(fs.readFileSync(path.join(contract, 'conformance/control/vectors.json')));
for (const vector of vectors) test('app control: ' + vector.name, () => runCase(vector));
test('app control: ignores uncorrelatable commands and reconnects with the Chrome 116 alarm', async () => {
  const app = await setup();
  try {
    const before = app.ports[0].sent.length;
    app.ports[0].onMessage.emit({ type: 'command', requestId: {}, action: 'start', tabId: 2, mode: 'tracks' });
    await new Promise(resolve => setImmediate(resolve)); assert.equal(app.audio.length, 0); assert.equal(app.ports[0].sent.length, before);
    app.ports[0].disconnect(); assert.equal(app.alarms.get('cassini-app-control').periodInMinutes, 1);
    app.chrome.alarms.onAlarm.emit({ name: 'cassini-app-control' }); assert.equal(app.ports.length, 2);
  } finally { app.close(); }
});

test('app control: a pending explicit-tab lookup reserves the same recording slot as the popup', async () => {
  const app = await setup();
  let release;
  try {
    const lookup = app.chrome.tabs.get; let entered = false;
    const gate = new Promise(resolve => { release = resolve; });
    app.chrome.tabs.get = async id => { entered = true; await gate; return lookup(id); };
    const recording = app.command({type:'command',requestId:'lookup',action:'start',tabId:2,mode:'tracks'});
    await until(() => entered);
    await assert.rejects(app.api.start('tracks'), /A recording is already running. Stop it before starting another./);
    release();assert.equal((await recording).ok,true);assert.equal(app.audio.length,1);
  } finally { release?.();app.close(); }
});

test('app control: default timers preserve their global receiver', async () => {
  const vm = require('node:vm');
  const context = vm.createContext({});
  vm.runInContext(`
    const scheduled=[];
    function setTimeout(fn, ms) { if(this !== globalThis) throw new TypeError('Illegal invocation'); scheduled.push({fn,ms}); return scheduled.length; }
    function clearTimeout(id) { if(this !== globalThis) throw new TypeError('Illegal invocation'); }
    globalThis.scheduled=scheduled;
  `,context);
  assert.throws(() => vm.runInContext('({cancel:clearTimeout}).cancel(null)', context), /Illegal invocation/);
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../app-control.js'),'utf8').replace('export class AppControl','class AppControl') + '\nglobalThis.Control=AppControl;',context);
  const { event } = require('./support/app-control-harness.cjs');
  const ports=[];
  const runtime={connectNative(){const port={onMessage:event(),onDisconnect:event(),postMessage(){},disconnect(){}};ports.push(port);return port}};
  const alarms={onAlarm:event(),async create(){},async clear(){}};
  const control=new context.Control({runtime,alarms,snapshot:async()=>[],command:async()=>({})});
  try {
    control.start();assert.equal(ports.length,1,'default cancel must not prevent connectNative');
    ports[0].onDisconnect.emit();assert.equal(context.scheduled.length,1,'default later schedules a reconnect');
    context.scheduled[0].fn();assert.equal(ports.length,2);
  } finally {control.close();}
});

for (const phase of ['discovery', 'inventory']) test('app control: lifecycle status waits for the initial snapshot during reconnect ' + phase, async () => {
  const app = await setup(); let release;
  try {
    const started = await app.command({ type:'command', requestId:'record', action:'start', tabId:2, mode:'tracks', independent:true, keepOnNavigation:true });
    assert.equal(started.ok,true); await app.api.appControl.publishing;
    const gate = new Promise(resolve => { release=resolve; }); let entered=false;
    const key = phase === 'discovery' ? 'query' : 'get', lookup = app.chrome.tabs[key];
    app.chrome.tabs[key] = async (...args) => { entered=true; await gate; return lookup(...args); };
    app.ports.at(-1).disconnect();
    app.chrome.alarms.onAlarm.emit({name:'cassini-app-control'});
    await until(()=>entered);
    const port = app.ports.at(-1);
    app.chrome.tabs.onUpdated.emit(2,{status:'loading'});
    assert.deepEqual(port.sent.map(packet=>packet.type),['hello'],'reconnect must not send status before initial meetings');
    release(); await until(()=>port.sent.some(packet=>packet.type==='status'));
    await app.api.appControl.publishing;
    assert.deepEqual(port.sent.slice(0,3).map(packet=>packet.type),['hello','meetings','status']);
    const before=port.sent.length;
    app.frame(2,0);
    assert.equal(port.sent[before].type,'status','initialized peers still receive the transition immediately');
    assert.equal(port.sent[before].sessions[0].pageState,'present');
  } finally {release?.(); app.close();}
});

const heartbeat = JSON.parse(fs.readFileSync(path.join(contract, 'conformance/control/heartbeat-vectors.json')));
test('app control: heartbeat-v1 answers each wire ping exactly, at once, without side effects', async () => {
  const app = await setup();
  try {
    const port = app.ports[0];
    assert.ok(port.sent[0].capabilities.includes('heartbeat-v1'));
    assert.ok(!port.sent.some(m => m.type === 'pong'), 'a pong is never unsolicited');
    for (const row of heartbeat.wire) {
      const before = port.sent.length, requests = app.api.appControl.requests.size;
      port.onMessage.emit(row.ping);
      // Synchronous: the reply is posted before receive() returns, not after any await.
      assert.deepEqual(port.sent.slice(before), row.reply ? [row.reply] : [], row.name);
      await new Promise(resolve => setImmediate(resolve));
      assert.equal(port.sent.length, before + (row.reply ? 1 : 0), row.name + ': no later reply');
      assert.equal(app.api.appControl.requests.size, requests, row.name + ': no command slot');
    }
    assert.equal(app.audio.length, 0, 'pings have no audio side effect');
    assert.equal(app.api.status().recording, false);
  } finally { app.close(); }
});

test('app control: a pong is not delayed behind a held command', async () => {
  const app = await setup(); let release;
  try {
    const lookup = app.chrome.tabs.get; let entered = false;
    const gate = new Promise(resolve => { release = resolve; });
    app.chrome.tabs.get = async id => { entered = true; await gate; return lookup(id); };
    const port = app.ports[0];
    const recording = app.command({ type: 'command', requestId: 'held', action: 'start', tabId: 2, mode: 'tracks' });
    await until(() => entered);
    // A second, legacy command now waits behind the held one: the queue is blocked.
    port.onMessage.emit({ type: 'command', requestId: 'queued', action: 'stop', tabId: 2 });
    const before = port.sent.length;
    port.onMessage.emit({ type: 'ping', seq: 41 });
    assert.deepEqual(port.sent.slice(before), [{ type: 'pong', seq: 41 }]);
    port.onMessage.emit({ type: 'ping', seq: 42 });
    assert.deepEqual(port.sent.slice(before), [{ type: 'pong', seq: 41 }, { type: 'pong', seq: 42 }]);
    assert.ok(!port.sent.some(m => m.type === 'result'), 'the held commands are still pending');
    release();
    assert.equal((await recording).ok, true);
    await until(() => port.sent.some(m => m.type === 'result' && m.requestId === 'queued'));
    assert.equal(app.audio.length, 1, 'only the start command touched audio');
    assert.equal(port.sent.filter(m => m.type === 'pong').length, 2, 'no extra pongs');
  } finally { release?.(); app.close(); }
});

test('app control: a ping resets the reconnect backoff; unknown app types are ignored', async () => {
  const app = await setup();
  try {
    const port = app.ports[0], control = app.api.appControl;
    control.delay = 16000;
    port.onMessage.emit({ type: 'ping', seq: 'bad' });
    assert.equal(control.delay, 16000, 'an invalid ping is ignored entirely');
    port.onMessage.emit({ type: 'ping', seq: 1 });
    assert.equal(control.delay, 1000);
    control.delay = 16000;
    const before = port.sent.length;
    for (const message of [{ type: 'heartbeat', seq: 1 }, { type: 'pong', seq: 1 }, { type: 'future-v9', requestId: 'x', action: 'start', tabId: 2, mode: 'tracks' }, null, 'ping'])
      port.onMessage.emit(message);
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(port.sent.length, before, 'unknown host types get no reply');
    assert.equal(control.delay, 16000, 'unknown host types change nothing');
    assert.equal(app.audio.length, 0);
  } finally { app.close(); }
});

test('app control: a peer without heartbeat-v1 never answers pings', async () => {
  const vm = require('node:vm');
  const context = vm.createContext({});
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../app-control.js'), 'utf8').replace('export class AppControl', 'class AppControl') + '\nglobalThis.Control=AppControl;', context);
  const { event } = require('./support/app-control-harness.cjs');
  const ports = [];
  const runtime = { connectNative() { const port = { sent: [], onMessage: event(), onDisconnect: event(), postMessage(m) { this.sent.push(m); }, disconnect() {} }; ports.push(port); return port; } };
  const alarms = { onAlarm: event(), async create() {}, async clear() {} };
  const control = new context.Control({ runtime, alarms, snapshot: async () => [], command: async () => ({}), capabilities: ['multi-stream-v1'], later: () => 1, cancel: () => {} });
  try {
    control.start();
    ports[0].onMessage.emit({ type: 'ping', seq: 1 });
    assert.deepEqual(ports[0].sent.map(m => m.type), ['hello']);
  } finally { control.close(); }
});
