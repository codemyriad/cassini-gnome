const { test } = require('node:test');
const assert = require('node:assert/strict');
const { setup, until } = require('./support/app-control-harness.cjs');
const command = (id, action, tabId, extra = {}) => ({type:'command',requestId:id,action,tabId,mode:'tracks',independent:true,...extra});
async function twoMeetings() {
  const app = await setup();
  app.tabs.set(3,{id:3,title:'Second meeting',url:'https://meet.example/second'});
  app.frame(3,2);
  return app;
}
test('independent capability starts two tabs, routes packets and stops only one', async () => {
  const app = await twoMeetings();
  try {
    assert.deepEqual(Array.from(app.ports[0].sent[0].capabilities), ['independent-sessions-v1']);
    assert.equal((await app.command(command('one','start',2,{captureLocal:false,microphone:true}))).ok,true);
    const second = await app.command(command('two','start',3,{captureLocal:false,microphone:false}));
    assert.equal(second.ok,true);assert.equal(app.audio.length,2);
    assert.deepEqual(second.status.sessions.map(s=>s.tabId).sort(),[2,3]);
    assert.equal(app.audio[0].sent[0].microphone,true);assert.equal(app.audio[1].sent[0].microphone,false);
    assert.ok(app.audio.every(port=>port.sent[0].independent && Number.isSafeInteger(port.sent[0].timelineOriginMs)));
    assert.ok(app.pages.every(frame=>frame.sent.find(m=>m.type==='start').captureLocal===false));
    const packet={type:'audio',id:'opaque',pcm:'AA==',frames:1};
    app.pages[1].onMessage.emit({type:'packet',request:1,packet});
    await until(()=>app.audio[1].sent.some(m=>m.id==='opaque'));
    assert.ok(!app.audio[0].sent.some(m=>m.id==='opaque'));
    const stopped=await app.command(command('stop-one','stop',2));
    assert.equal(stopped.ok,true);assert.equal(stopped.status.recording,false);
    assert.deepEqual(stopped.status.sessions.map(s=>s.tabId),[3]);
    assert.equal(app.audio[0].closed,true);assert.equal(app.audio[1].closed,false);
    assert.equal((await app.command(command('stop-again','stop',2))).ok,true);
    assert.equal(app.audio[1].closed,false);
    await app.command(command('stop-two','stop',3));
  } finally {app.close();}
});
test('duplicate start is refused without a second host; losing a tab leaves the other recording',async()=>{
  const app=await twoMeetings();
  try {
    await app.command(command('one','start',2));await app.command(command('two','start',3));
    assert.equal((await app.command(command('duplicate','start',2))).ok,false);assert.equal(app.audio.length,2);
    app.chrome.tabs.onRemoved.emit(2);
    await until(()=>app.audio[0].closed);
    assert.equal(app.audio[1].closed,false);
    assert.deepEqual(Array.from(app.api.controlStatus().sessions, s=>s.tabId),[3]);
    assert.equal(app.audio[0].sent.find(m=>m.type==='stop').failureReason,'lost');
    await app.command(command('stop','stop',3));
  } finally {app.close();}
});
test('a pending lookup reserves only its own tab in independent mode',async()=>{
  const app=await twoMeetings();let release;
  try {
    const get=app.chrome.tabs.get;let entered=false;
    const gate=new Promise(resolve=>release=resolve);
    app.chrome.tabs.get=async id=>{if(id===2){entered=true;await gate;}return get(id);};
    const first=app.api.start('tracks',2,{independent:true});await until(()=>entered);
    await app.api.start('tracks',3,{independent:true});assert.equal(app.audio.length,1);
    await assert.rejects(app.api.start('tracks',2,{independent:true}),/already running/);
    release();await first;assert.equal(app.audio.length,2);
    await app.command(command('stop-one','stop',2));await app.command(command('stop-two','stop',3));
  } finally {release?.();app.close();}
});

test('Stop cancels its pending independent lookup and never creates a late host',async()=>{
  const app=await setup();let release;
  try {
    const get=app.chrome.tabs.get;let entered=false;
    const gate=new Promise(resolve=>release=resolve);
    app.chrome.tabs.get=async id=>{entered=true;await gate;return get(id);};
    const start=app.command(command('pending','start',2));await until(()=>entered);
    const stop=app.command(command('cancel','stop',2));
    release();assert.equal((await start).ok,false);assert.equal((await stop).ok,true);
    assert.equal(app.audio.length,0);assert.equal(app.api.controlStatus().sessions.length,0);
  } finally {release?.();app.close();}
});
test('worker recovery reports all interrupted paths and never recreates capture',async()=>{
  const app=await twoMeetings();let next;
  try {
    await app.command(command('one','start',2));await app.command(command('two','start',3));
    const stored=JSON.parse(JSON.stringify(await app.chrome.storage.session.get()));
    assert.equal(stored.cassiniSessions.length,2);
    assert.notEqual(stored.cassiniSessions[0].captureId,stored.cassiniSessions[1].captureId);
    await app.command(command('s1','stop',2));await app.command(command('s2','stop',3));
    next=await setup(3,stored);
    const status=next.api.controlStatus();
    assert.equal(status.sessions.length,0);assert.equal(status.interrupted.length,2);
    assert.equal(next.audio.length,0);assert.ok(status.interrupted.every(s=>s.recording===false));
  } finally {next?.close();app.close();}
});
for(const vector of require('../../conformance/control/independent-vectors.json'))test('independent conformance: '+vector.name,async()=>{
  const app=await twoMeetings();
  try {
    for(const [index,input] of vector.commands.entries()){
      const result=await app.command({type:'command',requestId:'vector-'+index,...input});
      const expected=vector.expected[index];assert.equal(result.ok,expected.ok);
      assert.deepEqual(Array.from(app.api.controlStatus().sessions,s=>s.tabId),expected.tabs);
      assert.equal(app.audio.length,expected.hosts);
    }
  } finally {app.close();}
});
