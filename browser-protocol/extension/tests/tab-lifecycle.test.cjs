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
