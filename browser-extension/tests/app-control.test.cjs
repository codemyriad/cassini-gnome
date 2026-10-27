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
