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

test('only the popup-open check runs scripts in the meeting tab; refreshes read worker state alone',async()=>{
 const app=await setup(1);
 try {
  app.tabs.set(5,{id:5,title:'Talk',url:'https://meeting.test/call/x'});app.frame(5,1,0,'doc-5');
  await until(()=>[...app.api.frames].some(f=>f.tabId===5&&f.relayAlive&&f.hookReady));
  const reads=[];
  app.chrome.scripting={async executeScript(request){
   if(request.func?.name!=='readTalkParticipants')throw Error('unrelated read');
   reads.push(request);return [{documentId:request.target.documentIds[0],frameId:0,result:['Hypatia','‮Ibn al-Haytham']}];}};
  for(let i=0;i<3;i++)assert.equal((await rpc(app,{tabId:5})).participants,undefined,'plain refreshes never read the page');
  assert.equal(reads.length,0);
  const opened=await rpc(app,{tabId:5,fresh:true});
  assert.equal(reads.length,1);assert.equal(JSON.stringify(reads[0].target),JSON.stringify({tabId:5,documentIds:['doc-5']}));assert.equal(reads[0].world,'ISOLATED');
  assert.equal(JSON.stringify(opened.participants),JSON.stringify(['Hypatia','Ibn al-Haytham']));
  assert.equal((await rpc(app,{tabId:5})).participants,undefined);assert.equal(reads.length,1,'no read after the open check');
  // A reply for another document is never accepted, and a failing page yields an empty roster.
  app.chrome.scripting={async executeScript(request){return [{documentId:'other',frameId:0,result:['Mallory']}]}};
  assert.equal(JSON.stringify((await rpc(app,{tabId:5,fresh:true})).participants),'[]');
  app.chrome.scripting={async executeScript(){throw Error('Cannot access contents of this page')}};
  assert.equal(JSON.stringify((await rpc(app,{tabId:5,fresh:true})).participants),'[]');
 } finally {await cleanup(app)}
});

test('open popups are told when worker state changes, and nothing else may subscribe',async()=>{
 const app=await setup(1);
 try {
  const popupUrl=app.chrome.runtime.getURL('popup.html');
  const connect=(name,sender)=>{const port={name,sender,sent:[],onMessage:event(),onDisconnect:event(),postMessage(m){this.sent.push(m)}};app.chrome.runtime.onConnect.emit(port);return port};
  const popup=connect('cassini-popup',{id:'extension',url:popupUrl});
  const foreign=connect('cassini-popup',{id:'someone-else',url:popupUrl});
  const page=connect('cassini-popup',{id:'extension',url:'https://meeting.test/page',tab:{id:2}});
  const wrongName=connect('other',{id:'extension',url:popupUrl});
  app.tabs.set(6,{id:6,title:'New meeting',url:'https://meeting.test/new'});app.frame(6,1);
  await until(()=>popup.sent.length>0);
  assert.equal(popup.sent[0].type,'changed');
  for(const refused of [foreign,page,wrongName])assert.equal(refused.sent.length,0);
  popup.onDisconnect.emit();const seen=popup.sent.length;
  app.tabs.set(7,{id:7,title:'Another',url:'https://meeting.test/another'});app.frame(7,1);
  await until(()=>[...app.api.frames].some(f=>f.tabId===7&&f.hookReady));
  assert.equal(popup.sent.length,seen,'a closed popup is not written to');
 } finally {await cleanup(app)}
});
