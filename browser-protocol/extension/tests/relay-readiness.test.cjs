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
  const page=data=>{for(const fn of [...handlers.get('message')||[]])fn({isTrusted:true,source:window,data:{channel:'cassini-page',...data}})};
  return {ports,controls,handlers,runtime,timers,inject,page,context,invalidate(){alive=false}};
}
test('relay handshake belongs to the current worker instance; stale and incompatible replies never make it ready',()=>{
  const f=relay(),port=f.ports[0],responses=[];
  port.onMessage.emit({type:'relayHello',protocol:1,instanceId:'new-instance'});
  f.runtime.emit({type:'cassini-connect'},{},value=>responses.push(value));
  f.page({type:'hookReady',protocol:1,revision:3,instanceId:'old-instance'});
  f.page({type:'hookReady',protocol:2,revision:3,instanceId:'new-instance'});
  assert.equal(responses.length,0);
  f.page({type:'hookReady',protocol:1,revision:3,instanceId:'new-instance'});
  assert.equal(responses[0].connected,true);
  assert.equal(responses[0].instanceId,'new-instance');
  assert.equal(port.sent.filter(m=>m.type==='relayReady').length,1);
});
test('a hook from before the screen-share exclusion or WebAudio lineage (revision 1 or 2) is reload-required at once, never ready',()=>{
  const f=relay(),port=f.ports[0],responses=[];
  port.onMessage.emit({type:'relayHello',protocol:1,instanceId:'current'});
  f.runtime.emit({type:'cassini-connect'},{},value=>responses.push(value));
  // A page opened under 0.2.26 keeps that hook after an update: it answers without a revision.
  f.page({type:'hookReady',protocol:1,instanceId:'current'});
  assert.deepEqual(responses.map(r=>[r.connected,r.needsReload]),[[false,true]]);
  assert.equal(port.sent.filter(m=>m.type==='relayUnavailable').length,1);
  f.page({type:'hookReady',protocol:1,revision:1,instanceId:'current'});
  assert.equal(port.sent.some(m=>m.type==='relayReady'),false);
  // A page opened under 0.2.27 keeps a revision 2 hook without WebAudio microphone lineage.
  f.page({type:'hookReady',protocol:1,revision:2,instanceId:'current'});
  assert.equal(port.sent.some(m=>m.type==='relayReady'),false);
  assert.equal(port.sent.filter(m=>m.type==='relayUnavailable').length,1);
  assert.equal([...f.timers.values()].some(timer=>timer.ms===1500),false,'no handshake timer is left to fire');
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
    assert.deepEqual(JSON.parse(JSON.stringify(injections)),[{target:{tabId:2,allFrames:true},world:'ISOLATED'}]);
    assert.equal(typeof injections[0].func,'function');
    assert.equal(injections.some(value=>value.world==='MAIN'),false);
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
    await until(()=>injections===2&&timers.size===1);
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

for(const order of ['disconnect before pageshow','disconnect after pageshow'])
test('persisted restore replaces a frozen port and drains once: '+order,()=>{
 const f=relay(),old=f.ports[0];old.onMessage.emit({type:'start',token:'old'});
 f.page({type:'packet',token:'old',request:17,packet:{type:'audio'}});
 for(const fn of f.handlers.get('pagehide'))fn({isTrusted:true,persisted:true});
 old.postMessage=()=>{throw Error('Attempting to use a disconnected port object.')};
 if(order==='disconnect before pageshow')old.onDisconnect.emit();
 for(const fn of [...f.handlers.get('pageshow')])fn({isTrusted:true,persisted:true});
 assert.equal(f.ports.length,2,'pageshow must not reuse the cached closed port');
 assert.equal(f.context.__cassiniRelay.valid(),true,'a live extension realm must not retire on the stale port');
 if(order==='disconnect after pageshow')old.onDisconnect.emit();
 assert.equal(f.controls.filter(m=>m.type==='stop'&&m.token==='old').length,1);
 assert.equal(f.controls.filter(m=>m.type==='ack'&&m.token==='old'&&m.request===17&&m.error).length,1);
 const fresh=f.ports[1];fresh.onMessage.emit({type:'start',token:'new'});
 assert.equal(f.controls.filter(m=>m.type==='start').length,1,'new session waits for old hook flush');
 f.page({type:'stopped',token:'old'});
 assert.equal(f.controls.filter(m=>m.type==='start').length,2);
 assert.equal(f.controls.at(-1).token,'new');assert.equal(fresh.disconnected,undefined);
});

test('synthetic MessageEvent dispatch cannot claim hook readiness',()=>{
 const f=relay(),port=f.ports[0];
 port.onMessage.emit({type:'relayHello',protocol:1,instanceId:'current'});
 for(const listener of f.handlers.get('message'))listener({isTrusted:false,source:f.context.window,
  data:{channel:'cassini-page',type:'hookReady',protocol:1,revision:3,instanceId:'current'}});
 assert.equal(port.sent.some(message=>message.type==='relayReady'),false);
 f.page({type:'hookReady',protocol:1,revision:3,instanceId:'current'});
 assert.equal(port.sent.some(message=>message.type==='relayReady'),true);
});


test('synthetic pageshow/pagehide cannot reconnect or retire a live relay',()=>{
 const f=relay(),port=f.ports[0];
 port.onMessage.emit({type:'relayHello',protocol:1,instanceId:'current'});
 f.page({type:'hookReady',protocol:1,revision:3,instanceId:'current'});
 const before=port.sent.length;
 for(const name of ['pagehide','pageshow'])for(const listener of f.handlers.get(name))listener({isTrusted:false,persisted:true});
 assert.equal(f.ports.length,1);assert.equal(port.disconnected,undefined);assert.equal(port.sent.length,before);
 assert.equal(f.context.__cassiniRelay.valid(),true);
});

test('duplicate page hook-ready messages are forwarded only once per handshake',()=>{
 const f=relay(),port=f.ports[0];
 port.onMessage.emit({type:'relayHello',protocol:1,instanceId:'current'});
 for(let i=0;i<200;i++)f.page({type:'hookReady',protocol:1,revision:3,instanceId:'current'});
 assert.equal(port.sent.filter(message=>message.type==='relayReady').length,1);
});

test('the popup reload offered for a repaired relay is accepted; the app command keeps the strict rule',async()=>{
  const app=await setup(1),reloads=[];
  try {
    app.chrome.tabs.reload=async tabId=>reloads.push(tabId);
    const popup={id:'extension',url:app.chrome.runtime.getURL('popup.html')};
    const reload=(tabId,sender=popup)=>new Promise(resolve=>app.chrome.runtime.onMessage.emit({target:'background',type:'reloadMeetingTab',tabId},sender,resolve));
    // setup(1) gives tab 2 a healthy relay that is not document-start trusted: a repaired relay.
    assert.equal(app.api.controlStatus().browserHealth.tabs.find(t=>t.tabId===2).state,'ready');
    const strict=await app.command({type:'command',requestId:'strict',action:'reloadTab',tabId:2,independent:true});
    assert.equal(strict.ok,false);assert.equal(strict.code,'gone');assert.deepEqual(reloads,[]);
    const accepted=await reload(2);assert.equal(accepted.error,undefined);assert.deepEqual(reloads,[2]);
    assert.equal((await reload(99)).code,'gone','an unknown tab is still refused');
  } finally {app.close()}
});
test('a document-start trusted relay is never reloaded from the popup, nor a repaired one while recording',async()=>{
  const app=await setup(1),reloads=[];
  try {
    app.chrome.tabs.reload=async tabId=>reloads.push(tabId);
    const popup={id:'extension',url:app.chrome.runtime.getURL('popup.html')};
    const reload=tabId=>new Promise(resolve=>app.chrome.runtime.onMessage.emit({target:'background',type:'reloadMeetingTab',tabId},popup,resolve));
    const frame=[...app.api.frames].find(f=>f.tabId===2);frame.observerDocumentStart=true;
    assert.equal((await reload(2)).code,'gone');
    frame.observerDocumentStart=false;
    assert.equal((await app.command({type:'command',requestId:'go',action:'start',mode:'tracks',tabId:2,independent:true})).ok,true);
    assert.equal((await reload(2)).code,'busy');assert.deepEqual(reloads,[]);
    while(app.api.status().recording)await app.api.stop();
  } finally {app.close()}
});

test('departing capture drain longer than 1500 ms and 2500 ms keeps relay and worker checking until hookReady, and times out at 10000 ms if wedged',async()=>{
  const {moduleURL}=require('./support/page-capture-harness.cjs');
  const hookSource=fs.readFileSync(path.join(__dirname,'../page-hook.js'),'utf8');
  async function scenario(flushMs){
    const app=await setup(0);app.pages[0].onDisconnect.emit();
    let now=0,nextId=0;const queue=new Map();
    const later=(fn,ms)=>{const id=++nextId;queue.set(id,{fn,at:now+ms});return id};
    const cancel=id=>queue.delete(id);
    const advance=async ms=>{
      const target=now+ms;
      while(true){
        let earliest=null;
        for(const [id,item] of queue)if(item.at<=target&&(!earliest||item.at<earliest.at))earliest={id,...item};
        if(!earliest)break;
        queue.delete(earliest.id);now=earliest.at;earliest.fn();
        await new Promise(resolve=>setImmediate(resolve));
      }
      now=target;
      await new Promise(resolve=>setImmediate(resolve));
    };
    app.api.relayTimers.later=later;app.api.relayTimers.cancel=cancel;
    const listeners=new Map(),ports=[];
    class Peer{
      constructor(){this.connectionState='connected';this.iceConnectionState='connected';this.signalingState='stable';
        this.slots=[{currentDirection:'recvonly',receiver:{track:{id:'r1',kind:'audio',readyState:'live',enabled:true,muted:false},
          getStats:async()=>new Map([['rtp',{id:'rtp',type:'inbound-rtp',kind:'audio',packetsReceived:this.packets}]])}}];this.senders=[];this.packets=0}
      addEventListener(){}getTransceivers(){return this.slots}getSenders(){return this.senders}getReceivers(){return this.slots.map(s=>s.receiver)}close(){this.signalingState='closed'}
    }
    for(const name of ['getStats','createOffer','createAnswer','setLocalDescription','setRemoteDescription','addTrack','addTransceiver'])Peer.prototype[name]=()=>Promise.resolve();
    const window={location:{href:'https://meet.example/call'},RTCPeerConnection:Peer,
      addEventListener(name,fn){if(!listeners.has(name))listeners.set(name,new Set());listeners.get(name).add(fn)},
      removeEventListener(name,fn){listeners.get(name)?.delete(fn)},
      postMessage(data){queueMicrotask(()=>{for(const fn of [...listeners.get('message')||[]])fn({isTrusted:true,source:window,data})})}};
    let intervals=[];
    globalThis.__cassiniPageCapture=async options=>{
      await options.send({type:'track',id:options.id,label:options.label,kind:options.kind,sampleRate:48000,channels:1,sourceTrackId:options.sourceTrackId,clockMethod:'roundTrip',clockUncertaintyMs:1});
      return {failed:false,stop:()=>flushMs===Infinity?new Promise(()=>{}):new Promise(resolve=>later(resolve,flushMs))};
    };
    const hookContext=vm.createContext({window,navigator:{},document:{querySelectorAll:()=>[]},MediaStream:class{constructor(t){this.t=t}getAudioTracks(){return this.t}},
      MediaStreamTrack:class{},AudioContext:class{sampleRate=48000;state='running';async close(){this.state='closed'}},
      crypto:{randomUUID:()=>'span-1'},performance:{now:()=>now},setInterval:fn=>(intervals.push(fn),intervals.length),clearInterval(){},
      setTimeout:later,clearTimeout:cancel,queueMicrotask});
    vm.runInContext(hookSource,hookContext,{importModuleDynamically:vm.constants.USE_MAIN_CONTEXT_DEFAULT_LOADER});
    const pc=new window.RTCPeerConnection();await intervals[0]();pc.packets=15;await intervals[0]();pc.packets=30;await intervals[0]();
    function mountRelay(){
      let alive=true;const runtime=event();
      const chrome={runtime:{id:'extension',getManifest(){if(!alive)throw Error('Invalidated');return {version:'0.2.32'}},getURL:name=>name==='capture.js'?moduleURL:'extension://'+name,onMessage:runtime,
        connect(){
          const workerPort={name:'cassini-frame',sender:{id:'extension',tab:{id:2},frameId:0},onMessage:event(),onDisconnect:event(),sent:[],
            postMessage(m){this.sent.push(m);queueMicrotask(()=>relayPort.onMessage.emit(m))}};
          const relayPort={sent:[],onMessage:event(),onDisconnect:event(),disconnect(){this.disconnected=true;queueMicrotask(()=>workerPort.onDisconnect.emit())},
            postMessage(m){if(!alive)throw Error('Invalidated');this.sent.push(m);queueMicrotask(()=>workerPort.onMessage.emit(m))}};
          ports.push({workerPort,relayPort});app.chrome.runtime.onConnect.emit(workerPort);return relayPort;
        }}};
      const relayContext=vm.createContext({__cassiniRepairInjection:true,window,chrome,setTimeout:later,clearTimeout:cancel});
      vm.runInContext(source,relayContext);
      return {runtime,invalidate(){alive=false}};
    }
    try{
      const first=mountRelay();
      await until(()=>app.api.browserHealth().state==='ready');
      assert.equal((await app.command({type:'command',requestId:'start-1',action:'start',mode:'tracks',tabId:2,independent:true})).ok,true);
      await until(()=>app.audio[0].sent.some(p=>p.type==='track'));
      // Simulate extension reload/reconnect: retire the old relay (starting a slow drain) and mount a replacement relay.
      first.invalidate();ports[0].relayPort.onDisconnect.emit();ports[0].workerPort.onDisconnect.emit();
      await until(()=>!app.api.status().recording);
      const second=mountRelay(),probes=[];
      await new Promise(resolve=>setImmediate(resolve));
      await new Promise(resolve=>setImmediate(resolve));
      second.runtime.emit({type:'cassini-connect'},{},value=>probes.push(value));
      await advance(1500);
      assert.equal(ports[1].relayPort.sent.some(m=>m.type==='relayUnavailable'),false,'1500 ms timer must not fire relayUnavailable during active drain');
      assert.equal(probes.length,0,'probe waiter must wait for drain completion');
      assert.equal(app.api.browserHealth().state,'checking');
      await advance(1000); // now = 2500 ms (TAB_READY_DEADLINE_MS)
      assert.equal(app.api.browserHealth().state,'checking','2500 ms worker deadline must not mark draining hook reloadRequired');
      if(flushMs<Infinity){
        await advance(flushMs-2500);
        await until(()=>app.api.browserHealth().state==='ready');
        assert.equal(ports[1].relayPort.sent.some(m=>m.type==='relayUnavailable'),false);
        assert.equal(ports[1].relayPort.sent.some(m=>m.type==='relayReady'),true);
        assert.deepEqual(probes.map(p=>[p.connected,p.needsReload]),[[true,false]]);
      }else{
        await advance(7499); // now = 9999 ms
        assert.equal(app.api.browserHealth().state,'checking');
        await advance(1); // now = 10000 ms
        await until(()=>app.api.browserHealth().state==='reloadRequired');
        assert.equal(ports[1].relayPort.sent.some(m=>m.type==='relayUnavailable'),true);
        assert.deepEqual(probes.map(p=>[p.connected,p.needsReload]),[[false,true]]);
      }
    }finally{delete globalThis.__cassiniPageCapture;app.close()}
  }
  await scenario(4000);
  await scenario(Infinity);
});

test('same-relay reconnect during a >1500 ms drain waits up to 10000 ms before declaring relayUnavailable',()=>{
  const f=relay(),old=f.ports[0];
  old.onMessage.emit({type:'start',token:'departing'});
  for(const fn of f.handlers.get('pagehide'))fn({isTrusted:true,persisted:true});
  for(const fn of [...f.handlers.get('pageshow')])fn({isTrusted:true,persisted:true});
  const fresh=f.ports[1];
  fresh.onMessage.emit({type:'relayHello',protocol:1,instanceId:'restored'});
  assert.equal([...f.timers.values()].some(t=>t.ms===1500),false,'active drainingToken must not arm a 1500 ms handshake timeout');
  const drainTimer=[...f.timers.values()].find(t=>t.ms===10000);
  assert.ok(drainTimer,'draining handshake uses 10000 ms bound');
  f.page({type:'stopped',token:'departing'});
  f.page({type:'hookReady',protocol:1,revision:3,instanceId:'restored'});
  assert.equal(fresh.sent.some(m=>m.type==='relayUnavailable'),false);
  assert.equal(fresh.sent.some(m=>m.type==='relayReady'),true);
});
