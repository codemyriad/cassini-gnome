const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const {setup,event,until}=require('./support/app-control-harness.cjs');
const source=fs.readFileSync(path.join(__dirname,'../relay.js'),'utf8');
function relay() {
  const handlers=new Map(), controls=[], ports=[], timers=new Map();let alive=true,id=0;
  const runtime=event();
  const window={addEventListener(name,fn){if(!handlers.has(name))handlers.set(name,new Set());handlers.get(name).add(fn)},
    removeEventListener(name,fn){handlers.get(name)?.delete(fn)},postMessage:m=>controls.push(m)};
  const chrome={runtime:{id:'extension',getManifest(){if(!alive)throw Error('Extension context invalidated.');return {version:'0.2.17'}},getURL:p=>'extension://'+p,onMessage:runtime,
    connect(){const port={sent:[],onMessage:event(),onDisconnect:event(),disconnect(){this.disconnected=true},postMessage(m){if(!alive)throw Error('Extension context invalidated.');this.sent.push(m)}};ports.push(port);return port}}};
  const context=vm.createContext({window,chrome,setTimeout(fn,ms){const key=++id;timers.set(key,{fn,ms});return key},clearTimeout:key=>timers.delete(key)});
  const inject=()=>vm.runInContext(source,context);inject();
  const page=data=>{for(const fn of [...handlers.get('message')||[]])fn({source:window,data:{channel:'cassini-page',...data}})};
  return {ports,controls,handlers,runtime,timers,inject,page,context,invalidate(){alive=false}};
}
test('relay handshake belongs to the current worker instance; stale and incompatible replies never make it ready',()=>{
  const f=relay(),port=f.ports[0],responses=[];
  port.onMessage.emit({type:'relayHello',protocol:1,instanceId:'new-instance'});
  f.runtime.emit({type:'cassini-connect'},{},value=>responses.push(value));
  f.page({type:'hookReady',protocol:1,instanceId:'old-instance'});
  f.page({type:'hookReady',protocol:2,instanceId:'new-instance'});
  assert.equal(responses.length,0);
  f.page({type:'hookReady',protocol:1,instanceId:'new-instance'});
  assert.equal(responses[0].connected,true);
  assert.equal(responses[0].instanceId,'new-instance');
  assert.equal(port.sent.filter(m=>m.type==='relayReady').length,1);
});
test('an old hook times out explicitly and later probes receive reload-required without hanging',()=>{
  const f=relay(),port=f.ports[0],responses=[];
  port.onMessage.emit({type:'relayHello',protocol:1,instanceId:'current'});
  f.runtime.emit({type:'cassini-connect'},{},value=>responses.push(value));
  [...f.timers.values()].find(timer=>timer.ms===1500).fn();
  assert.equal(responses[0].needsReload,true);
  assert.equal(responses[0].connected,false);
  f.runtime.emit({type:'cassini-connect'},{},value=>responses.push(value));
  assert.equal(responses.length,2);
});
test('reinjection is idempotent, and an invalidated idle relay removes its listeners instead of swallowing the failure',()=>{
  const f=relay();f.inject();
  assert.equal(f.ports.length,1);
  assert.equal([...f.handlers.values()].reduce((n,set)=>n+set.size,0),3);
  f.invalidate();
  assert.doesNotThrow(()=>f.page({type:'discovery',sourceCount:1,heard:true}));
  assert.equal([...f.handlers.values()].reduce((n,set)=>n+set.size,0),0);
  assert.equal(f.ports[0].disconnected,true);
  assert.equal(f.context.__cassiniRelay.valid(),false);
});
test('invalidated recording relay drains both admitted and late requests before tearing down',()=>{
  const f=relay(),port=f.ports[0];port.onMessage.emit({type:'start',token:'old'});
  f.page({type:'packet',token:'old',request:1,packet:{type:'audio'}});
  f.invalidate();port.onDisconnect.emit();
  assert.equal(f.controls.filter(m=>m.type==='stop').length,1,'one stop for the departing token');
  f.page({type:'packet',token:'old',request:2,packet:{type:'audio'}});
  for(const request of [1,2])assert.ok(f.controls.some(m=>m.type==='ack'&&m.request===request&&m.error));
  f.page({type:'stopped',token:'old'});
  assert.equal([...f.handlers.values()].reduce((n,set)=>n+set.size,0),0);
});
function unreadyFrame(app,tabId=2,frameId=5,count=1,outcome="relayUnavailable") {
  const port={name:'cassini-frame',sender:{id:'extension',tab:{id:tabId},frameId},onMessage:event(),onDisconnect:event(),sent:[],postMessage(m){this.sent.push(m);if(m.type==='stop')queueMicrotask(()=>this.onMessage.emit({type:'stopped'}))}};
  app.chrome.runtime.onConnect.emit(port);
  const hello=port.sent.find(m=>m.type==='relayHello');
  port.onMessage.emit({...hello});
  port.onMessage.emit({type:'discovery',sourceCount:count,heard:true});
  if(outcome)port.onMessage.emit({...hello,type:outcome});
  return {port,hello};
}
test('native connection and per-tab readiness are separate; idle browsers never get a reload warning',async()=>{
  const app=await setup(0);
  try {
    let health=app.api.controlStatus().browserHealth;
    assert.equal(health.nativeControl,'connected');assert.equal(health.state,'idle');assert.deepEqual([...health.tabs],[]);
    const {port,hello}=unreadyFrame(app);
    health=app.api.controlStatus().browserHealth;
    assert.equal(health.state,'reloadRequired');assert.equal(health.tabs[0].hookSeen,true);assert.equal(health.tabs[0].relayAlive,true);
    assert.equal((await app.api.meetings()).meetings.length,0,'an unready hook cannot advertise an auto-start candidate');
    port.onMessage.emit({...hello,type:'relayReady',instanceId:'stale'});
    assert.equal(app.api.controlStatus().browserHealth.state,'reloadRequired');
    port.onMessage.emit({...hello,type:'relayReady'});
    assert.equal(app.api.controlStatus().browserHealth.state,'ready');
    assert.equal((await app.api.meetings()).meetings[0].sourceCount,1);
    port.onMessage.emit({type:'discovery',sourceCount:0,heard:false});
    app.api.appControl.port.disconnect();
    health=app.api.controlStatus().browserHealth;
    assert.equal(health.state,'idle');assert.equal(health.nativeControl,'disconnected');
  } finally {app.close()}
});
test('lost meeting subframe cannot be hidden by healthy or idle unrelated frames; tab close clears its diagnostic',async()=>{
  const app=await setup(0);
  try {
    const remote=app.frame(2,1,4);remote.onDisconnect.emit();
    await app.api.repairTab(2,4);
    app.pages[0].onMessage.emit({type:'discovery',sourceCount:0,heard:false});
    assert.equal(app.api.controlStatus().browserHealth.state,'reloadRequired');
    app.chrome.tabs.onRemoved.emit(2);
    assert.equal(app.api.controlStatus().browserHealth.state,'idle');
  } finally {app.close()}
});
test('recovery injects only a relay, reuses compatible instances, and degrades restricted tabs without warning about idle pages',async()=>{
  const app=await setup(0);const injections=[];
  try {
    app.chrome.tabs.sendMessage=async()=>null;
    app.chrome.scripting={async executeScript(value){injections.push(value);throw Error('Cannot access contents of this page')}};
    await app.api.repairTab(2);
    assert.deepEqual(JSON.parse(JSON.stringify(injections)),[{target:{tabId:2,allFrames:true},files:['relay.js'],world:'ISOLATED'}]);
    assert.equal(app.api.controlStatus().browserHealth.state,'idle');
    const instance=app.api.controlStatus().browserHealth.extensionInstance;
    app.chrome.tabs.sendMessage=async()=>({connected:true,protocol:1,instanceId:instance});
    await app.api.repairTab(2);assert.equal(injections.length,1);
  } finally {app.close()}
});
test('explicit reload action is limited to a detected broken meeting and never starts native audio',async()=>{
  const app=await setup(0),reloads=[];
  try {
    app.chrome.tabs.reload=async tabId=>reloads.push(tabId);
    unreadyFrame(app);
    const command=tabId=>({type:'command',requestId:'reload-'+tabId,action:'reloadTab',tabId,independent:true});
    const result=await app.command(command(2));
    assert.equal(result.ok,true);
    assert.equal(result.status.browserHealth.state,'idle');
    const duplicate=await app.command({...command(2),requestId:'reload-again'});
    assert.equal(duplicate.ok,false);assert.equal(duplicate.code,'gone');
    assert.equal((await app.command(command(99))).ok,false);
    assert.deepEqual(reloads,[2]);assert.equal(app.audio.length,0);
    const hello=app.ports[0].sent.find(m=>m.type==='hello');
    assert.equal(hello.extensionInstance,app.api.controlStatus().browserHealth.extensionInstance);
    assert.ok(hello.capabilities.includes('tab-readiness-v1'));
  } finally {app.close()}
});

test('a relay that never answers cannot hold recovery or the first status forever',async()=>{
  const app=await setup(0),timers=new Map();let id=0,injections=0;
  try {
    app.api.relayTimers.later=(fn,ms)=>{const key=++id;timers.set(key,{fn,ms});return key};
    app.api.relayTimers.cancel=key=>timers.delete(key);
    app.chrome.tabs.sendMessage=()=>new Promise(()=>{});
    app.chrome.scripting={async executeScript(){injections++}};
    const repair=app.api.repairTab(2);
    await until(()=>timers.size===1);assert.equal([...timers.values()][0].ms,2500);
    [...timers.values()][0].fn();
    await until(()=>injections===1&&timers.size===1);
    [...timers.values()][0].fn();
    await repair;
    assert.equal(app.api.browserHealth().state,'idle');
  } finally {app.close()}
});
test('completed incompatible-hook diagnosis stays visible while its current relay is re-probed',async()=>{
  const app=await setup(0);let respond;
  try {
    unreadyFrame(app);
    app.chrome.tabs.sendMessage=()=>new Promise(resolve=>{respond=resolve});
    app.chrome.scripting={async executeScript(){assert.fail('a current incompatible hook requires reload, not repeated reinjection')}};
    const repair=app.api.repairTab(2);
    await until(()=>respond);
    assert.equal(app.api.browserHealth().state,'reloadRequired');
    respond({connected:false,needsReload:true,protocol:1,instanceId:app.api.browserHealth().extensionInstance});
    await repair;
    assert.equal(app.api.browserHealth().state,'reloadRequired');
  } finally {app.close()}
});
test('the extension instance survives worker eviction and changes for a new extension load',async()=>{
  const first=await setup(0),stored=await first.chrome.storage.session.get();
  const next=await setup(0,stored,[],null,'worker-'),reloaded=await setup(0,{},[],null,'reload-');
  try {
    assert.equal(next.api.browserHealth().extensionInstance,first.api.browserHealth().extensionInstance);
    assert.notEqual(reloaded.api.browserHealth().extensionInstance,first.api.browserHealth().extensionInstance);
  } finally {first.close();next.close();reloaded.close()}
});

test('a browser-confirmed removed meeting frame clears its warning instead of accusing an idle browser',async()=>{
  const app=await setup(0);
  try {
    app.chrome.tabs.sendMessage=async()=>null;
    app.chrome.scripting={async executeScript(){throw Error('No frame with id 4 in tab 2')}};
    const remote=app.frame(2,1,4);remote.onDisconnect.emit();
    await app.api.repairTab(2,4);
    assert.equal(app.api.browserHealth().state,'idle');
  } finally {app.close()}
});

for(const state of ['unavailable','checking','lost'])test('whole-tab readiness gates idle inventory and both track Start paths: healthy + '+state,async()=>{
  const app=await setup(1);
  const command=id=>({type:'command',requestId:id,action:'start',tabId:2,mode:'tracks',independent:true});
  let broken;
  try {
    const query=app.chrome.tabs.query;
    app.chrome.tabs.query=async options=>options.active?[await app.chrome.tabs.get(2)]:query(options);
    app.pages[0].onMessage.emit({type:'discovery',sourceCount:1,heard:true});
    if(state==='lost') {
      const old=app.frame(2,1,5);old.onDisconnect.emit();await app.api.repairTab(2,5);
    } else {
      broken=unreadyFrame(app,2,5,1,state==='checking'?null:'relayUnavailable');
    }
    assert.equal(app.api.browserHealth().tabs.find(t=>t.tabId===2).state,state==='checking'?'checking':'reloadRequired');
    assert.equal((await app.api.meetings()).meetings.some(m=>m.tabId===2),false);
    const refused=await app.command(command('unready'));
    assert.equal(refused.ok,false);assert.equal(refused.code,'pageUnreachable');
    await assert.rejects(app.api.start('tracks'),error=>error.resultCode==='pageUnreachable','popup track Start uses the same tab gate');
    assert.equal(app.audio.length,0,'no native Start for a healthy subset');
    assert.equal(app.api.status().recording,false);
    if(state==='lost') app.frame(2,1,5);
    else broken.port.onMessage.emit({...broken.hello,type:'relayReady'});
    assert.equal(app.api.browserHealth().tabs.find(t=>t.tabId===2).state,'ready');
    assert.equal((await app.api.meetings()).meetings.find(m=>m.tabId===2).sourceCount,2);
    assert.equal((await app.command(command('recovered'))).ok,true);
    assert.equal(app.audio.length,1);
    // Health may degrade during a retained recording. Inventory/status retain
    // that session, and an attempted Start must remain busy rather than fallback.
    unreadyFrame(app,2,9);
    assert.equal(app.api.browserHealth().tabs.find(t=>t.tabId===2).state,'reloadRequired');
    assert.equal((await app.api.meetings()).meetings.some(m=>m.tabId===2),true);
    assert.equal(app.api.controlStatus().sessions[0].recording,true);
    const duplicate=await app.command(command('busy'));
    assert.equal(duplicate.ok,false);assert.equal(duplicate.code,'busy');
    await assert.rejects(app.api.start('tracks'),error=>error.resultCode==='busy');
    assert.equal(app.audio.length,1);assert.equal(app.audio[0].sent.filter(p=>p.type==='start').length,1);
    assert.equal(app.audio[0].sent.some(p=>p.type==='stop'),false,'health never fabricates a Stop');
    await app.api.stop();
  } finally {app.close()}
});

test('idle inventory rechecks whole-tab readiness after asynchronous tab lookup',async()=>{
 const app=await setup(1);let release;
 try {
  let entered;const lookupEntered=new Promise(resolve=>entered=resolve),gate=new Promise(resolve=>release=resolve);
  const get=app.chrome.tabs.get;
  app.chrome.tabs.get=async id=>{entered();await gate;return get(id)};
  const inventory=app.api.meetings();await lookupEntered;
  unreadyFrame(app);release();
  assert.equal((await inventory).meetings.some(m=>m.tabId===2),false);
 } finally {release?.();app.close()}
});


for(const phase of ['lookup','connect'])test('track Start rechecks tab readiness after asynchronous '+phase,async()=>{
 const app=await setup(1);
 try {
  let injected=false;
  const key=phase==='lookup'?'get':'sendMessage',original=app.chrome.tabs[key];
  app.chrome.tabs[key]=async(...args)=>{if(!injected){injected=true;unreadyFrame(app)}return original(...args)};
  const result=await app.command({type:'command',requestId:'raced-start',action:'start',tabId:2,mode:'tracks',independent:true});
  assert.equal(injected,true);assert.equal(result.ok,false);assert.equal(result.code,'pageUnreachable');
  assert.equal(app.audio.length,0);
 } finally {app.close()}
});

test('an unready meeting in another tab does not block a healthy tab',async()=>{
 const app=await setup(1);
 try {
  app.tabs.set(3,{id:3,title:'Other meeting',url:'https://meet.example/other'});
  unreadyFrame(app,3);
  assert.equal(app.api.browserHealth().state,'reloadRequired');
  assert.deepEqual([...(await app.api.meetings()).meetings].map(m=>m.tabId),[2]);
  const result=await app.command({type:'command',requestId:'healthy-tab',action:'start',tabId:2,mode:'tracks',independent:true});
  assert.equal(result.ok,true);assert.equal(app.audio.length,1);
  await app.api.stop();
 } finally {app.close()}
});
