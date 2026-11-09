// Real worker/control boundary; no browser, input device or output allocation.
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {setup,until}=require('./support/app-control-harness.cjs');
const root=path.resolve(__dirname,'../..');
const contract=fs.existsSync(path.join(root,'conformance/control/call-signals-vectors.json'))?root:path.join(root,'browser-protocol');
const vectors=JSON.parse(fs.readFileSync(path.join(contract,'conformance/control/call-signals-vectors.json'))).nativeStartCorrelation;
for(const vector of vectors.cases)test('native Start request binding: '+vector.name,async()=>{
 const app=await setup();let release,entered=false,held=true;
 const get=app.chrome.tabs.get,gate=new Promise(resolve=>release=resolve);
 app.chrome.tabs.get=async id=>{if(held&&id===2){held=false;entered=true;await gate}return get(id)};
 app.api.callInfo(2).generation='call-A';
 try{
  const pending=app.command(vector.commands[0]);
  await until(()=>entered);
  assert.equal(app.audio.length,0,'delayed discovery must not build a native packet early');
  release();assert.equal((await pending).ok,true);
  for(const command of vector.commands.slice(1))assert.equal((await app.command(command)).ok,true);
  const native=app.audio.map(port=>port.sent.find(packet=>packet.type==='start'));
  assert.equal(native.length,2);
  for(let index=0;index<native.length;index++){
   assert.equal(native[index].requestId,vector.expectedNativeStarts[index].requestId);
   assert.equal(native[index].callGeneration,'call-A','same ongoing call cannot distinguish these asks');
  }
  // Transport delivery may reorder these packets. The producer preserves the
  // exact ID/type in either order; actual host retirement/dispatch is downstream.
  assert.deepEqual(vector.nativeArrivalOrder.map(index=>native[index].requestId),
   [vector.pendingManualRequestId,vector.retiredAutomaticRequestId]);
 }finally{release?.();app.close()}
});
test('a direct popup/manual Start invents no host requestId',async()=>{
 const app=await setup();
 try{
  await app.api.start('tracks',2,{independent:true});
  assert.equal(Object.hasOwn(app.audio[0].sent[0],'requestId'),false);
 }finally{app.close()}
});
