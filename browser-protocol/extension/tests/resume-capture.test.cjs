const { test } = require('node:test');
const assert = require('node:assert/strict');
const { setup, event, until } = require('./support/app-control-harness.cjs');
const fs = require('node:fs'), path = require('node:path');
const root = path.resolve(__dirname,'../..');
const contract = fs.existsSync(path.join(root,'conformance/control/vectors.json')) ? root : path.join(root,'browser-protocol');
const shared = JSON.parse(fs.readFileSync(path.join(contract,'conformance/control/resume-capture-vectors.json')));
const vector = name => shared.cases.find(row=>row.name===name);
function nativeTypes(f, name) {assert.deepEqual(f.packets.map(p=>p.type),vector(name).expectedNativeTypes);}

async function fixture() {
  const job = { active:true, captureId:'stand-in-F', origin:1000, path:'/test/stand-in', microphoneFrames:96000,
    selection:{me:true,others:true}, generation:null };
  const packets = [], deferred = [], observations = [];
  let defer = false, badProof = false, proofPatch = {};
  const app = await setup(3, {}, [], () => {
    const port = { sent:[], closed:false, onMessage:event(), onDisconnect:event(),
      disconnect() { this.closed = true; },
      postMessage(packet) {
        this.sent.push(packet); packets.push(packet);
        const answer = () => {
          if (packet.type === 'resume') {
            if (!job.active || packet.captureId !== job.captureId || packet.timelineOriginMs !== job.origin || packet.callGeneration !== job.generation)
              return this.onMessage.emit({type:'error',code:'state',scope:'command',message:'Target is no longer active.'});
            observations.push('nativeAccepted');
            this.onMessage.emit({type:'started',path:job.path,features:['source-outcomes-v1','resume-capture-v1'],resumed:!badProof,
              captureId:job.captureId,timelineOriginMs:job.origin,callGeneration:job.generation,...proofPatch});
          } else if (packet.type === 'start') {
            this.onMessage.emit({type:'started',path:'/test/fresh',features:['source-outcomes-v1']});
          } else if (packet.type === 'stop') {
            if (packet.captureId === job.captureId) job.active = false;
            this.onMessage.emit({type:'stopped',path:job.path});
          } else this.onMessage.emit({type:'ack'});
        };
        if (defer && packet.type === 'resume') deferred.push(answer); else queueMicrotask(answer);
      }
    };
    return port;
  });
  const controlPort = app.ports[0], publish = controlPort.postMessage;
  controlPort.postMessage = function(packet) {
    if (packet.type === 'status' && packet.recording) observations.push('status');
    return publish.call(this,packet);
  };
  const pendingCommands = new Set(), command = app.command;
  app.command = message => {
    const result = command(message); pendingCommands.add(result);
    result.finally(() => pendingCommands.delete(result)).catch(() => {});
    return result;
  };
  const close = async () => {
    deferred.splice(0).forEach(fn=>fn());
    await Promise.allSettled([...pendingCommands]); app.close();
  };
  job.generation = app.api.callInfo(2).generation;
  const start = id => ({type:'command',requestId:id,action:'start',mode:'tracks',tabId:2,independent:true,
    callGeneration:job.generation,captureLocal:false,microphone:false,resumeCapture:{captureId:job.captureId,timelineOriginMs:job.origin}});
  const stop = id => ({type:'command',requestId:id,action:'stop',tabId:2,independent:true,captureId:job.captureId});
  return {app,job,packets,deferred,observations,start,stop,close,set defer(value){defer=value;},set badProof(value){badProof=value;},set proofPatch(value){proofPatch=value;}};
}

test('resume uses the original identity/origin in early status, native request and page capture', async () => {
  const f = await fixture(); f.defer = true;
  try {
    const pending = f.app.command(f.start('resume'));
    await until(() => f.deferred.length === 1);
    assert.equal(f.app.api.status().captureId, 'stand-in-F');
    assert.equal(f.app.api.controlStatus(2).timelineOriginMs, 1000);
    assert.equal(f.packets[0].type, 'resume');
    assert.equal(f.packets[0].captureId, 'stand-in-F');
    assert.equal(f.packets[0].timelineOriginMs, 1000);
    assert.equal(Object.hasOwn(f.packets[0], 'microphone'), false);
    await until(()=>f.observations.includes('status'));
    f.deferred.shift()();
    assert.equal((await pending).ok, true);
    assert.ok(f.observations.indexOf('status') < f.observations.indexOf('nativeAccepted'));
    const page = f.app.pages[0].sent.find(p => p.type === 'start');
    assert.equal(page.token, 'stand-in-F'); assert.equal(page.origin, 1000);
    assert.equal(f.app.api.status().path, '/test/stand-in');
    assert.equal(f.job.microphoneFrames, 96000); assert.deepEqual(f.job.selection, {me:true,others:true});
    assert.equal(f.packets.filter(p => p.type === 'start').length, 0);
    assert.equal((await f.app.command(f.stop('stop'))).ok, true);
    assert.equal(f.job.active, false);
    nativeTypes(f, 'status-before-native');
  } finally {f.deferred.splice(0).forEach(fn=>fn());await f.close();}
});

for(const scoped of [false,true]) test('Stop cancels a resume queued before construction; fresh Record remains allowed (scoped='+scoped+')', async () => {
  const f = await fixture();
  try {
    const port = f.app.ports[0];
    port.onMessage.emit(f.start('resume-queued'));
    const stopping = f.stop('stop-queued'); if(!scoped) delete stopping.captureId;
    port.onMessage.emit(stopping);
    await until(() => port.sent.some(p=>p.type==='result'&&p.requestId==='stop-queued'));
    assert.equal(port.sent.find(p=>p.type==='result'&&p.requestId==='resume-queued').ok,false);
    assert.equal(port.sent.find(p=>p.type==='result'&&p.requestId==='stop-queued').ok,true);
    nativeTypes(f,'stop-before-construction');
    const command = f.start('fresh'); delete command.resumeCapture;
    assert.equal((await f.app.command(command)).ok,true);
    assert.equal(f.packets[0].type,'start'); assert.notEqual(f.packets[0].captureId,'stand-in-F');
    const token = f.app.api.status().captureId;
    assert.equal((await f.app.command(f.stop('stale-F-stop'))).ok,false);
    assert.equal(f.app.api.status().captureId,token);
    await f.app.command({...f.stop('fresh-stop'),captureId:token});
    nativeTypes(f,'fresh-record-after-stop');
  } finally {await f.close();}
});

test('Stop with the original captureId cancels a resumed Start during tab lookup', async () => {
  const f = await fixture(); let release;
  try {
    const get = f.app.chrome.tabs.get;
    let entered = false;
    const gate = new Promise(resolve=>release=resolve);
    f.app.chrome.tabs.get = async id => {if(id===2){entered=true;await gate;}return get(id);};
    const pending = f.app.command(f.start('lookup'));
    await until(()=>entered);
    const stopped = f.app.command(f.stop('stop-lookup'));
    release();
    assert.equal((await pending).ok,false); assert.equal((await stopped).ok,true);
    nativeTypes(f,'stop-during-lookup');
  } finally {release?.();await f.close();}
});

test('native Stop preceding a delayed resume refuses it without making a new recording', async () => {
  const f = await fixture(); f.defer = true;
  try {
    const pending = f.app.command(f.start('late-native'));
    await until(()=>f.deferred.length===1);
    f.job.active = false; // The app's independent native Stop commits first.
    f.deferred.shift()();
    assert.equal((await pending).ok,false);
    nativeTypes(f,'stop-before-native');
    assert.equal(f.app.api.status().recording,false);
    assert.equal(f.job.microphoneFrames,96000);
  } finally {f.deferred.splice(0).forEach(fn=>fn());await f.close();}
});

test('Stop during resume ACK finalizes the accepted target, never an alias', async () => {
  const f = await fixture(); f.defer = true;
  try {
    const pending = f.app.command(f.start('ack'));
    await until(()=>f.deferred.length===1);
    const stopped = f.app.command(f.stop('stop-ack'));
    f.deferred.shift()();
    assert.equal((await pending).ok,false); assert.equal((await stopped).ok,true);
    assert.equal(f.job.active,false);
    nativeTypes(f,'stop-during-ack');
    assert.deepEqual(f.packets.map(p=>[p.type,p.captureId]),[['resume','stand-in-F'],['stop','stand-in-F']]);
  } finally {f.deferred.splice(0).forEach(fn=>fn());await f.close();}
});

test('a missing explicit resume proof cannot attach tracks or Stop the still-healthy target', async () => {
  const f = await fixture(); f.badProof = true;
  try {
    assert.equal((await f.app.command(f.start('bad-proof'))).ok,false);
    assert.equal(f.job.active,true);
    assert.deepEqual(f.packets.map(p=>p.type),['resume']);
    assert.ok(!f.app.pages[0].sent.some(p=>p.type==='start'));
  } finally {await f.close();}
});

test('a changed call generation refuses resume before opening a native transport', async () => {
  const f = await fixture();
  try {
    const command = f.start('old-call'); command.callGeneration = 'old-A';
    assert.equal((await f.app.command(command)).ok,false);
    nativeTypes(f,'stale-call-A'); assert.equal(f.job.active,true);
  } finally {await f.close();}
});

for (const change of [{independent:false},{callGeneration:undefined},{resumeCapture:{captureId:'',timelineOriginMs:1000}},
  {resumeCapture:{captureId:'é'.repeat(65),timelineOriginMs:1000}},{resumeCapture:{captureId:'F',timelineOriginMs:NaN}}])
  test('invalid resume is rejected without native side effects: '+JSON.stringify(change), async () => {
    const f = await fixture();
    try {assert.equal((await f.app.command({...f.start('invalid'),...change})).ok,false);assert.equal(f.packets.length,0);}
    finally {await f.close();}
  });


test('native acceptance can precede delayed control status without changing the target', async () => {
  const f = await fixture(); let release;
  const snapshot = f.app.api.appControl.snapshot;
  const gate = new Promise(resolve=>release=resolve);
  f.app.api.appControl.snapshot = async () => {await gate;return snapshot();};
  try {
    assert.equal((await f.app.command(f.start('native-first'))).ok,true);
    assert.deepEqual(f.observations,['nativeAccepted']);
    release(); await until(()=>f.observations.includes('status'));
    assert.equal(f.app.api.controlStatus(2).captureId,vector('native-before-status').expectedCaptureId);
    assert.equal(f.app.api.controlStatus(2).timelineOriginMs,vector('native-before-status').expectedOrigin);
    assert.equal((await f.app.command(f.stop('native-first-stop'))).ok,true);
    nativeTypes(f,'native-before-status');
  } finally {release();await f.close();}
});


for(const patch of [{captureId:'other'}, {timelineOriginMs:1001}, {callGeneration:'other'},
                    {features:['source-outcomes-v1']}, {path:null}, {type:'ack'}])
  test('mismatched native proof cannot attach or stop the target: '+JSON.stringify(patch),async()=>{
    const f=await fixture();f.proofPatch=patch;
    try {
      assert.equal((await f.app.command(f.start('proof'))).ok,false);
      assert.equal(f.job.active,true);assert.deepEqual(f.packets.map(p=>p.type),['resume']);
      assert.ok(!f.app.pages[0].sent.some(p=>p.type==='start'));
    }finally{await f.close();}
  });
