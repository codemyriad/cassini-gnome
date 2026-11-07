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
