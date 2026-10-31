// SPDX-License-Identifier: GPL-3.0-or-later
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {setup, until} = require('./support/app-control-harness.cjs');
const start = (tabId=2) => ({type:'command',requestId:'start-'+tabId,action:'start',mode:'tracks',tabId,independent:true,keepOnNavigation:true});
const stop = (tabId=2,reason='user') => ({type:'command',requestId:'stop-'+tabId,action:'stop',tabId,independent:true,reason});
const plain = value => JSON.parse(JSON.stringify(value));

test('opted-in reload keeps one native connection, capture token and original clock',async()=>{
 const app=await setup();
 try {
  assert.equal((await app.command(start())).ok,true);
  const native=app.audio[0], first=app.pages[0].sent.find(p=>p.type==='start');
  app.chrome.tabs.onUpdated.emit(2,{status:'loading'});
  app.pages[0].onMessage.emit({type:'discovery',sourceCount:3,heard:true});
  assert.equal((await app.api.meetings()).meetings[0].sourceCount,0,'old document cannot revive counts');
  app.pages[0].onMessage.emit({type:'pageState',state:'present'});
  app.pages[0].onMessage.emit({type:'error'});
  assert.equal(native.closed,false,'an unloading document cannot terminate the retained native path');
  assert.equal(app.api.controlStatus().sessions[0].pageState,'navigating');
  app.pages[0].onDisconnect.emit();
  assert.equal(native.closed,false);assert.equal(app.api.controlStatus().sessions.length,1);
  assert.equal(app.api.controlStatus().sessions[0].pageState,'navigating');
  const returned=app.frame(2,2), next=returned.sent.find(p=>p.type==='start');
  assert.equal(next.token,first.token);assert.equal(next.origin,first.origin);
  assert.equal(app.audio.length,1);assert.equal(native.sent.filter(p=>p.type==='start').length,1);
  assert.equal(app.api.controlStatus().sessions[0].pageState,'present');
  assert.equal((await app.api.meetings()).meetings[0].sourceCount,2);
  returned.onMessage.emit({type:'packet',request:1,packet:{type:'track',id:'returned-source',kind:'remote',sampleRate:48000,channels:1}});
  await until(()=>native.sent.some(p=>p.id==='returned-source'));
  assert.equal((await app.command(stop())).ok,true);
  assert.equal(native.sent.find(p=>p.type==='stop').reason,'user');
 } finally {app.close();}
});

test('a closed tab stays closed after late page traffic and stops only when its app requests it',async()=>{
 const app=await setup();
 try {
  app.tabs.set(3,{id:3,title:'Other call',url:'https://meet.example/second'});app.frame(3,2);
  await app.command(start());await app.command(start(3));
  app.tabs.delete(2);app.chrome.tabs.onRemoved.emit(2);
  assert.equal(app.audio[0].closed,false,'the app owns the 2s close grace');
  assert.deepEqual(plain(app.api.controlStatus().sessions.map(s=>[s.tabId,s.pageState])),[[2,'closed'],[3,'present']]);
  app.pages[0].onMessage.emit({type:'pageState',state:'present'});
  app.pages[0].onDisconnect.emit();
  assert.equal(app.api.controlStatus().sessions[0].pageState,'closed');
  assert.equal((await app.command(stop(2,'tabClosed'))).ok,true);
  assert.equal(app.audio[0].sent.find(p=>p.type==='stop').reason,'tabClosed');
  assert.equal(app.audio[0].sent.find(p=>p.type==='stop').failureReason,undefined);
  assert.equal(app.audio[1].closed,false);
  await app.command(stop(3));
 } finally {app.close();}
});

test('legacy independent starts still finalize on loss; lifecycle state cannot be page-spoofed closed',async()=>{
 const app=await setup();
 try {
  const command=start();delete command.keepOnNavigation;
  await app.command(command);
  app.pages[0].onMessage.emit({type:'pageState',state:'closed'});
  assert.equal(app.api.controlStatus().sessions[0].pageState,'present');
  app.pages[0].onDisconnect.emit();await until(()=>app.audio[0].closed);
  assert.equal(app.audio[0].sent.find(p=>p.type==='stop').failureReason,'lost');
 } finally {app.close();}
});

test('same-document URL change preserves audio; only top-frame lifecycle changes tab state',async()=>{
 const app=await setup();
 try {
  await app.command(start());
  app.chrome.tabs.onUpdated.emit(2,{url:'https://meet.example/call#speakers'});
  assert.equal((await app.api.meetings()).meetings[0].sourceCount,3);
  assert.equal(app.api.controlStatus().sessions[0].pageState,'present');
  const child=app.frame(2,0,1);child.onMessage.emit({type:'pageState',state:'navigating'});
  assert.equal(app.api.controlStatus().sessions[0].pageState,'present');
  app.pages[0].onMessage.emit({type:'pageState',state:'navigating'});
  assert.equal(app.api.controlStatus().sessions[0].pageState,'navigating');
  app.pages[0].onMessage.emit({type:'pageState',state:'present'});
  assert.equal(app.api.controlStatus().sessions[0].pageState,'present');
  await app.command(stop());
 } finally {app.close();}
});

test('invalid lifecycle command options have no native side effects',async()=>{
 const app=await setup();
 try {
  for(const extra of [{keepOnNavigation:'true'},{independent:false},{reason:'user'}])
   assert.equal((await app.command({...start(),requestId:JSON.stringify(extra),...extra})).ok,false);
  assert.equal(app.audio.length,0);
  await app.command(start());
  for(const reason of ['unknown',null,1])
   assert.equal((await app.command({...stop(),requestId:'invalid-'+reason,reason})).ok,false);
  assert.equal(app.audio[0].closed,false);
  await app.command(stop());
 } finally {app.close();}
});


test('a previous gap warning does not rewrite the explicit terminal Stop reason',async()=>{
 const app=await setup();
 try {
  await app.command(start());
  app.pages[0].onMessage.emit({type:'warning'});
  assert.equal((await app.command(stop())).ok,true);
  const packet=app.audio[0].sent.find(p=>p.type==='stop');
  assert.ok(packet.problem);assert.equal(packet.reason,'user');
 } finally {app.close();}
});

test('fast reload publishes navigation before a blocked asynchronous inventory can coalesce it away',async()=>{
 const app=await setup();let release;
 try {
  await app.command(start());await app.api.appControl.publishing;
  const port=app.ports.at(-1),before=port.sent.length;
  const lookup=app.chrome.tabs.get;
  let entered=false;const gate=new Promise(resolve=>{release=resolve;});
  app.chrome.tabs.get=async id=>{entered=true;await gate;return lookup(id);};
  app.api.appControl.publish();await until(()=>entered);
  app.chrome.tabs.onUpdated.emit(2,{status:'loading'});
  app.pages[0].onDisconnect.emit();app.frame(2,0);
  const states=port.sent.slice(before).filter(p=>p.type==='status').map(p=>p.sessions.find(s=>s.tabId===2)?.pageState);
  assert.ok(states.includes('navigating'),'active navigation evidence must not wait for chrome.tabs.get');
  assert.ok(states.indexOf('navigating')<states.lastIndexOf('present'),'return follows the published navigation');
  release();await app.api.appControl.publishing;
  await app.command(stop());
 } finally {release?.();app.close();}
});

test('inventory recomputes source evidence after tab lookup instead of mixing old counts with the returned document',async()=>{
 const app=await setup();let release;
 try {
  await app.command(start());await app.api.appControl.publishing;
  const lookup=app.chrome.tabs.get;let entered=false;
  const gate=new Promise(resolve=>{release=resolve;});
  app.chrome.tabs.get=async id=>{entered=true;await gate;return lookup(id);};
  const pending=app.api.meetings();await until(()=>entered);
  app.chrome.tabs.onUpdated.emit(2,{status:'loading'});
  app.pages[0].onDisconnect.emit();app.frame(2,0);
  release();const snapshot=await pending;
  const current=snapshot.meetings.find(m=>m.tabId===2);
  assert.equal(current.sourceCount,0);assert.equal(current.heard,false);assert.equal(current.pageState,'present');
  await app.api.appControl.publishing;await app.command(stop());
 } finally {release?.();app.close();}
});

test('all tab lookups settle before any tab source evidence is collected',async()=>{
 const app=await setup();let release;
 try {
  app.frame(99,2);
  assert.equal((await app.command(start(2))).ok,true);
  assert.equal((await app.command(start(99))).ok,true);
  await app.api.appControl.publishing;
  const port=app.ports.at(-1),before=port.sent.length;
  const lookup=app.chrome.tabs.get;let aResolved=false,bPending=false;
  const gate=new Promise(resolve=>{release=resolve;});
  app.chrome.tabs.get=async id=>{
   if(id===99){bPending=true;await gate;}
   const tab=await lookup(id);
   if(id===2)aResolved=true;
   return tab;
  };
  app.api.appControl.publish();await until(()=>aResolved&&bPending);
  // Flush already-ready A's promise continuations; B remains explicitly gated.
  await new Promise(resolve=>setImmediate(resolve));
  app.chrome.tabs.onUpdated.emit(2,{status:'loading'});
  app.pages[0].onDisconnect.emit();app.frame(2,0);
  const states=port.sent.slice(before).filter(p=>p.type==='status').map(p=>p.sessions.find(s=>s.tabId===2)?.pageState);
  assert.ok(states.includes('navigating'));
  assert.ok(states.indexOf('navigating')<states.lastIndexOf('present'));
  release();await app.api.appControl.publishing;
  const inventories=port.sent.slice(before).filter(p=>p.type==='meetings');
  assert.ok(inventories.length>=2,'the pending publication and dirty refresh both complete');
  for(const packet of inventories){
   const a=packet.meetings.find(row=>row.tabId===2),b=packet.meetings.find(row=>row.tabId===99);
   assert.equal(a.pageState,'present');
   assert.equal(a.sourceCount,0,'A must not publish its pre-reload count after the navigation/return statuses');
   assert.equal(a.heard,false);
   assert.equal(b.sourceCount,2,'B keeps its independent source evidence');
  }
  await app.command(stop(2));await app.command(stop(99));
 } finally {release?.();app.close();}
});
