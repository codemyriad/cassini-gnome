const {test}=require('node:test');
const assert=require('node:assert/strict');
const {setup,event,until}=require('./support/app-control-harness.cjs');
const rpc=(app,message,sender={id:'extension',url:app.chrome.runtime.getURL('popup.html')})=>new Promise(resolve=>
  app.chrome.runtime.onMessage.emit({target:'background',type:'status',...message},sender,resolve));
const command=tabId=>({type:'command',requestId:'start-'+tabId,action:'start',mode:'tracks',tabId,independent:true});
async function cleanup(app){while(app.api.status().recording)await app.api.stop();app.close()}

test('scoped popup status selects either recording and never leaks the first session into an idle tab',async()=>{
 const app=await setup(1);
 try {
  app.tabs.set(3,{id:3,title:'Second meeting',url:'https://meeting.test/second'});app.frame(3,1);
  for(const id of [2,3])assert.equal((await app.command(command(id))).ok,true);
  const legacy=await rpc(app,{});assert.equal(legacy.tabId,2);assert.equal('starting' in legacy,false);
  for(const id of [2,3]){
   const result=await rpc(app,{tabId:id});assert.equal(result.tabId,id);assert.equal(result.recording,true);assert.equal(result.starting,false);
   assert.equal(result.captureId,app.api.controlStatus().sessions.find(s=>s.tabId===id).captureId);
  }
  const idle=await rpc(app,{tabId:99});
  assert.equal(idle.tabId,99);assert.equal(idle.recording,false);assert.equal(idle.starting,false);assert.equal(idle.path,null);assert.equal(idle.tracks,0);
  assert.equal('captureId' in idle,false);assert.ok(idle.browserHealth);
  const port={name:'cassini-frame',sender:{id:'extension',tab:{id:3},frameId:5},onMessage:event(),onDisconnect:event(),sent:[],postMessage(m){this.sent.push(m)}};
  app.chrome.runtime.onConnect.emit(port);const hello=port.sent.find(m=>m.type==='relayHello');port.onMessage.emit(hello);
  port.onMessage.emit({type:'discovery',sourceCount:1,heard:true});port.onMessage.emit({...hello,type:'relayUnavailable'});
  const broken=await rpc(app,{tabId:3});
  assert.equal(broken.recording,true);assert.equal(broken.tabId,3);assert.equal(broken.browserHealth.tabs.find(t=>t.tabId===3).state,'reloadRequired');
  await app.api.stop(); // Stop the first recording; targeted idle must not inherit its saved status.
  const stopped=await rpc(app,{tabId:2});assert.equal(stopped.recording,false);assert.equal(stopped.tabId,2);assert.equal(stopped.path,null);
  assert.equal((await rpc(app,{})).tabId,3,'omitted ID keeps first-session semantics');
 } finally {await cleanup(app)}
});

for(const phase of ['lookup','native','storage'])test('scoped status exposes starting until Start completes: '+phase,async()=>{
 let release,entered;const gate=new Promise(resolve=>release=resolve),arrived=new Promise(resolve=>entered=resolve);
 const app=await setup(1);let starting;
 try {
  if(phase==='lookup'){
   const get=app.chrome.tabs.get;app.chrome.tabs.get=async id=>{entered();await gate;return get(id)};
  } else if(phase==='storage'){
   const set=app.chrome.storage.session.set;app.chrome.storage.session.set=async value=>{if(value.cassiniActive){entered();await gate}return set(value)};
  } else {
   const connect=app.chrome.runtime.connectNative;
   app.chrome.runtime.connectNative=name=>{
    const port=connect(name),emit=port.onMessage.emit;
    if(name==='io.codemyriad.cassini')port.onMessage.emit=message=>{if(message.type==='started'){entered();void gate.then(()=>emit(message))}else emit(message)};
    return port;
   };
  }
  starting=app.command(command(2));await arrived;
  const scoped=await rpc(app,{tabId:2});assert.equal(scoped.starting,true);assert.equal(scoped.recording,false);assert.equal(scoped.tabId,2);
  assert.equal((await rpc(app,{tabId:99})).starting,false);
  if(phase!=='lookup')assert.equal((await rpc(app,{})).recording,true,'legacy in-flight recording flag is unchanged');
  release();assert.equal((await starting).ok,true);
  const ready=await rpc(app,{tabId:2});assert.equal(ready.starting,false);assert.equal(ready.recording,true);
 } finally {release();await starting;await cleanup(app)}
});

test('an untargeted popup Start is scoped once the browser resolves its actual tab',async()=>{
 const app=await setup(1);let release,starting;
 const gate=new Promise(resolve=>release=resolve);
 try {
  const query=app.chrome.tabs.query;app.chrome.tabs.query=async options=>options.active?[app.tabs.get(2)]:query(options);
  let entered=false;
  app.chrome.tabCapture.getMediaStreamId=async()=> 'fixture-stream';
  app.chrome.runtime.sendMessage=async()=>({});
  app.chrome.offscreen.createDocument=async()=>{entered=true;await gate};
  starting=app.api.start('tab');await until(()=>entered);
  assert.equal((await rpc(app,{})).recording,true,'legacy ready flag precedes offscreen setup completion');
  const result=await rpc(app,{tabId:2});assert.equal(result.starting,true);assert.equal(result.recording,false);
  release();await starting;
  const ready=await rpc(app,{tabId:2});assert.equal(ready.starting,false);assert.equal(ready.recording,true);
 } finally {release();await starting;await cleanup(app)}
});

test('invalid scoped IDs return errors without falling back to another recording',async()=>{
 const app=await setup(1);
 try {
  await app.command(command(2));
  for(const tabId of [undefined,null,-1,1.5,Number.MAX_SAFE_INTEGER+1,NaN,'2',{},[]]){
   const result=await rpc(app,{tabId});assert.equal(result.code,'invalid');assert.ok(result.error);assert.equal('recording' in result,false);assert.equal('browserHealth' in result,false);
  }
  const zero=await rpc(app,{tabId:0});assert.equal(zero.tabId,0);assert.equal(zero.recording,false);
 } finally {await cleanup(app)}
});

test('targeted status remains popup-only and a foreign extension receives no response',async()=>{
 const app=await setup(1);
 try {
  await app.command(command(2));
  for(const url of [app.chrome.runtime.getURL('offscreen.html'),app.chrome.runtime.getURL('other.html'),'https://meeting.test/']){
   const result=await rpc(app,{tabId:2},{id:'extension',url});assert.ok(result.error);assert.equal('recording' in result,false);assert.equal('browserHealth' in result,false);
  }
  let response;app.chrome.runtime.onMessage.emit({target:'background',type:'status',tabId:2},{id:'other-extension',url:app.chrome.runtime.getURL('popup.html')},value=>response=value);
  await new Promise(resolve=>setImmediate(resolve));assert.equal(response,undefined);
 } finally {await cleanup(app)}
});
