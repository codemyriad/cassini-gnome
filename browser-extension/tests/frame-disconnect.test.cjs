const {test}=require('node:test');const assert=require('node:assert/strict');
const {setup,until}=require('./support/app-control-harness.cjs');
const bfcache='The page keeping the extension port is moved into back/forward cache, so the message channel is closed.';
function disconnect(app,port,message) {
 let reads=0;
 Object.defineProperty(app.chrome.runtime,'lastError',{configurable:true,get(){reads++;return message===undefined?undefined:{message}}});
 try {port.onDisconnect.emit();return reads;}
 finally {delete app.chrome.runtime.lastError;}
}
for(const message of [bfcache,'The frame unexpectedly disappeared.',undefined]) {
 test('frame disconnect consumes callback lastError and removes idle frame: '+String(message),async()=>{
  const app=await setup(0);
  try {
   const port=app.pages[0],frame=[...app.api.frames].find(f=>f.port===port);let finished=0;frame.finished=()=>finished++;
   assert.equal(disconnect(app,port,message),1,'read the receiving callback error synchronously');
   assert.equal(app.api.frames.has(frame),false);assert.equal(finished,1);
   assert.equal(app.api.browserHealth().state,'idle');assert.equal((await app.api.meetings()).meetings.length,0);
   assert.equal(app.audio.length,0);
  }finally{app.close()}
 });
 test('consuming lastError preserves ordinary capture loss: '+String(message),async()=>{
  const app=await setup(3);
  try {
   assert.equal((await app.command({type:'command',requestId:'start',action:'start',mode:'tracks',tabId:2,independent:true})).ok,true);
   assert.equal(disconnect(app,app.pages[0],message),1);
   await until(()=>app.audio[0].sent.some(p=>p.type==='stop')&&!app.api.status().recording);
   const stops=app.audio[0].sent.filter(p=>p.type==='stop');
   assert.equal(stops.length,1);assert.equal(stops[0].failureReason,'lost');assert.equal(stops[0].cause,'frameClosed');
   assert.equal(app.audio[0].closed,true);assert.equal(app.api.frames.size,0);
   await app.api.repairTab(2,0);assert.equal(app.api.browserHealth().tabs[0].state,'reloadRequired');
  }finally{await app.api.stop();app.close()}
 });
}

test('BFCache-like retained disconnect keeps the same capture and accepts a fresh handshaken relay',async()=>{
 const app=await setup(3);
 try {
  const started=await app.command({type:'command',requestId:'keep',action:'start',mode:'tracks',tabId:2,independent:true,keepOnNavigation:true});
  assert.equal(started.ok,true);const captureId=started.status.captureId;
  app.chrome.tabs.onUpdated.emit(2,{status:'loading'});
  assert.equal(disconnect(app,app.pages[0],bfcache),1);
  assert.equal(app.api.frames.size,0);assert.equal(app.api.status().recording,true);assert.equal(app.api.status().pageState,'navigating');
  assert.equal(app.audio[0].sent.some(p=>p.type==='stop'),false);
  const restored=app.frame(2,3);
  assert.equal(app.api.status().captureId,captureId);assert.equal(app.api.status().pageState,'present');
  assert.equal(app.api.browserHealth().tabs[0].state,'ready');
  assert.equal(restored.sent.filter(p=>p.type==='start').length,1);
  assert.equal(app.audio.length,1);assert.equal(app.audio[0].sent.filter(p=>p.type==='start').length,1);
 }finally{await app.api.stop();app.close()}
});

test('a zero-receiver child frame still receives Start, so its microphone and rendered media record, and an empty one leaving never ends the session',async()=>{
 const app=await setup(3,{},['source-outcomes-v1']);
 const send=async(page,packet,id)=>{page.onMessage.emit({type:'packet',request:id,packet});await until(()=>page.sent.some(m=>m.type==='ack'&&m.request===id));return page.sent.find(m=>m.type==='ack'&&m.request===id)};
 try {
  const mediaChild=app.frame(2,0,4),emptyChild=app.frame(2,0,5);
  assert.equal((await app.command({type:'command',requestId:'start',action:'start',mode:'tracks',tabId:2,independent:true})).ok,true);
  for(const page of [app.pages[0],mediaChild,emptyChild])assert.equal(page.sent.some(p=>p.type==='start'),true,'every ready frame receives start, as on main');
  // A send-only microphone and the far-end mix rendered by a child have no incoming RTP receiver there.
  assert.equal((await send(mediaChild,{type:'track',id:'child-mic',kind:'local',sampleRate:48000,channels:1},11)).error,undefined);
  assert.equal((await send(mediaChild,{type:'track',id:'child-mix',kind:'media',sampleRate:48000,channels:1},12)).error,undefined);
  const recorded=app.audio[0].sent.filter(p=>p.type==='track').map(p=>p.kind).sort();
  assert.deepEqual(recorded,['local','media'],'the child frame audio reaches the native host');
  assert.equal((await send(app.pages[0],{type:'track',id:'main-remote',kind:'remote',sampleRate:48000,channels:1},1)).error,undefined);
  // A child that never contributed leaves: the capture goes on. One that contributed ends it, as before.
  assert.equal(disconnect(app,emptyChild,undefined),1);
  assert.equal(app.api.status().recording,true);assert.equal(app.audio[0].closed,false);
  assert.equal((await send(app.pages[0],{type:'audio',id:'main-remote',sequence:0,offsetMs:0,pcm:'AAA='},2)).error,undefined);
  assert.equal((await app.command({type:'command',requestId:'stop',action:'stop',tabId:2,independent:true})).ok,true);
  assert.equal(app.audio[0].sent.find(p=>p.type==='stop').failureReason,undefined);
 }finally{await app.api.stop();app.close()}
});

test('a child frame whose first track is still waiting for the native acknowledgement counts as contributing when it disconnects',async()=>{
 const app=await setup(3,{},['source-outcomes-v1']);
 try {
  const child=app.frame(2,0,4);
  assert.equal((await app.command({type:'command',requestId:'start',action:'start',mode:'tracks',tabId:2,independent:true})).ok,true);
  const host=app.audio[0],original=host.postMessage,held=[];
  host.postMessage=function(packet){if(packet.type==='track'){held.push(()=>original.call(host,packet));return;}return original.call(host,packet)};
  child.onMessage.emit({type:'packet',request:7,packet:{type:'track',id:'child-first',kind:'remote',sampleRate:48000,channels:1}});
  await until(()=>held.length===1);
  assert.equal(disconnect(app,child,undefined),1,'the child goes away before its first track is acknowledged');
  for(const release of held.splice(0))release();
  await until(()=>!app.api.status().recording);
  assert.equal(app.api.status().recording,false,'losing a contributing child is reported, not treated as an empty child');
 }finally{await app.api.stop();app.close()}
});

test('an attached child frame that contributed no sources does not end main capture on disconnect, while a contributing child frame does',async()=>{
 const app=await setup(2,{},['source-outcomes-v1']);
 const send=async(page,packet,id)=>{page.onMessage.emit({type:'packet',request:id,packet});await until(()=>page.sent.some(m=>m.type==='ack'&&m.request===id));return page.sent.find(m=>m.type==='ack'&&m.request===id)};
 try {
  const idleChild=app.frame(2,1,4);
  assert.equal((await app.command({type:'command',requestId:'start',action:'start',mode:'tracks',tabId:2,independent:true})).ok,true);
  assert.equal(idleChild.sent.some(p=>p.type==='start'),true);
  assert.equal((await send(app.pages[0],{type:'track',id:'main-voice',kind:'remote',sampleRate:48000,channels:1},1)).error,undefined);
  idleChild.onMessage.emit({type:'discovery',sourceCount:0,heard:false});
  assert.equal(disconnect(app,idleChild,undefined),1);
  assert.equal(app.api.status().stopping,false,'empty departing child must not fail main capture');
  assert.equal(app.api.status().recording,true,'empty departing child must not fail main capture');
  assert.equal(app.audio[0].closed,false);
  const speakingChild=app.frame(2,1,6);
  await until(()=>speakingChild.sent.some(p=>p.type==='start'));
  assert.equal((await send(speakingChild,{type:'track',id:'child-voice',kind:'remote',sampleRate:48000,channels:1},2)).error,undefined);
  assert.equal(disconnect(app,speakingChild,undefined),1);
  await until(()=>app.audio[0].closed&&!app.api.status().recording);
  const stop=app.audio[0].sent.find(p=>p.type==='stop');
  assert.equal(stop.failureReason,'lost');
  assert.equal(stop.cause,'frameClosed');
 }finally{await app.api.stop();app.close()}
});
