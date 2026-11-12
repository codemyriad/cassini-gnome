// Actual worker/AppControl with modeled browser script results; no real inputs.
const {test}=require('node:test');const assert=require('node:assert/strict');
const {setup,until,event}=require('./support/app-control-harness.cjs');
const pair=['call-signals-v1','verified-auto-admission-v1'];
async function fixture(trusted=true){
  let clock=0,reads=0;
  const fixtureDate=class extends Date {static now(){return clock}};
  const app=await setup(0,{},[],null,'token-',null,{Date:fixtureDate});
  app.api.discoveryTimers.now=()=>clock;
  app.pages[0].onDisconnect.emit();
  const port=app.frame(2,1,0,'chrome-A');
  const hello=port.sent.find(message=>message.type==='relayHello');
  port.onMessage.emit({...hello,type:'relayReady',observerDocumentStart:trusted});
  const raw={version:2,cohortQualified:true,documentGeneration:'hook-A',hookGeneration:'0',revision:2,ageMs:0,
    receivedPeers:1,capturedSendingPeers:1,samePeerCount:1,
    receivedVersion:1,sentVersion:1,ended:false,sourceCount:1,heard:true};
  app.chrome.scripting={async executeScript(request){
    if(request.world==='MAIN'){reads++;return [{frameId:0,documentId:'chrome-A',result:{...raw}}]}
    return [{frameId:0,documentId:'chrome-A'}];
  }};
  port.onMessage.emit({type:'discovery',sourceCount:1,heard:true,documentGeneration:'hook-A',callGeneration:'0'});
  if(trusted)await until(()=>[...app.api.frames].some(frame=>frame.documentGeneration==='hook-A'));
  app.api.genericProducer.now=()=>clock;
  const control=app.api.appControl,wire=app.ports[0].sent;
  async function publish(){control.publishSignals();await until(()=>!control.signalRun)}
  async function configure(capabilities=pair){control.port.onMessage.emit({type:'configure',capabilities});await until(()=>!control.signalRun);await control.publishing}
  const last=()=>wire.filter(message=>message.type==='callSignals').at(-1);
  return {app,raw,port,control,wire,publish,configure,last,setClock(value){clock=value},get reads(){return reads}};
}
test('worker advertises pair, publishes current namespace mapping only after ACK, and preserves raw RTP',async()=>{
  const f=await fixture();
  try{
    assert.ok(f.wire[0].capabilities.includes('verified-auto-admission-v1'));
    assert.equal('browserDocumentId' in (await f.app.api.meetings()).meetings[0],false);
    assert.equal(f.wire.some(message=>message.type==='callSignals'),false);
    await f.configure();
    const row=f.last().tabs[0],meeting=(await f.app.api.meetings({verified:true})).meetings[0];
    assert.equal(row.callEvidence.state,'active');assert.equal(row.documentGeneration,'chrome-A');
    assert.equal(row.sourceDocumentGeneration,'hook-A');assert.notEqual(row.callEvidence.callGeneration,'0');
    assert.equal(meeting.browserDocumentId,row.documentGeneration);assert.equal(meeting.documentGeneration,row.sourceDocumentGeneration);
    assert.equal(meeting.callGeneration,row.callEvidence.callGeneration);
    assert.equal(meeting.sourceCount,1);assert.equal(meeting.heard,true);
    await f.configure(['call-signals-v1']);assert.equal(f.last().tabs.length,0);
    await f.control.publishing;
    assert.equal('browserDocumentId' in f.wire.filter(message=>message.type==='meetings').at(-1).meetings[0],false);
  }finally{f.app.close()}
});
test('only new bidirectional progress renews unchanged active rows; pong never renews proof',async()=>{
  const f=await fixture();
  try{
    await f.configure();const first=f.last();
    f.setClock(1000);await f.publish();assert.equal(f.last().sequence,first.sequence);
    f.raw.receivedVersion++;f.raw.sentVersion++;f.raw.revision++;await f.publish();
    assert.equal(f.last().tabs[0].callEvidence.state,'active');assert.ok(f.last().sequence>first.sequence);
    const count=f.wire.filter(message=>message.type==='callSignals').length;
    f.control.port.onMessage.emit({type:'ping',seq:9});
    assert.equal(f.wire.at(-1).type,'pong');assert.equal(f.wire.filter(message=>message.type==='callSignals').length,count);
    f.setClock(4000);f.raw.ageMs=3000;await f.publish();
    assert.equal(f.last().tabs[0].callEvidence.state,'unknown');assert.equal(f.last().tabs[0].callEvidence.callGeneration,null);
    assert.equal((await f.app.api.meetings()).meetings[0].heard,true);
    const timer=f.app.timers.get(f.control.signalTimer);assert.ok(timer&&timer.ms<=1000);
  }finally{f.app.close()}
});
test('publisher timer withdraws frozen proof within its virtual bound despite healthy pongs', {timeout:10000}, async()=>{
  const f=await fixture();
  try{
    await f.configure();const initial=f.last().sequence,port=f.control.port;
    const later=f.control.later;let rearmed;
    f.control.later=(fn,ms)=>{const id=later(fn,ms);rearmed?.();return id};
    for(const at of [1000,2000,3000]){
      f.setClock(at);f.raw.ageMs=at;
      port.onMessage.emit({type:'ping',seq:at});
      assert.equal(f.wire.at(-1).type,'pong');assert.equal(f.wire.at(-1).seq,at);
      const id=f.control.signalTimer,timer=f.app.timers.get(id);
      assert.ok(timer&&timer.ms<=1000,'the actual publisher schedules a refresh within one second');
      const next=new Promise(resolve=>{rearmed=resolve});
      f.app.timers.delete(id);timer.fn();await next;
      assert.equal(f.control.port,port);assert.equal(port.closed,false);
      if(at===1000){assert.equal(f.last().sequence,initial);assert.equal(f.last().tabs[0].callEvidence.state,'active')}
    }
    assert.equal(f.last().tabs[0].callEvidence.state,'unknown');
    assert.equal(f.last().tabs[0].callEvidence.callGeneration,null);
    assert.ok(f.last().sequence>initial);
  }finally{f.app.close()}
});
test('repaired/old frames and forged discovery facts cannot create verified call evidence',async()=>{
  const f=await fixture(false);
  try{
    f.port.onMessage.emit({type:'discovery',sourceCount:1,heard:true,documentGeneration:'hook-A',callGeneration:'0',
      observerDocumentStart:true,callEvidence:{state:'active'},samePeerCount:1});
    await f.configure();assert.equal(f.last().tabs[0].callEvidence.state,'unknown');assert.equal(f.reads,0);
  }finally{f.app.close()}
});
test('explicit terminal endpoint end retains generation for callEnded; a new observed episode rotates only background identity',async()=>{
  const f=await fixture();
  try{
    await f.configure();const first=f.last().tabs[0];
    f.raw.ended=true;f.raw.cohortQualified=false;f.raw.samePeerCount=0;f.raw.receivedPeers=0;f.raw.capturedSendingPeers=0;f.raw.ageMs=null;
    f.setClock(3000);await f.publish();const end=f.last().tabs[0];
    assert.equal(end.callEvidence.state,'inactive');assert.equal(end.callEvidence.reason,'callEnded');
    assert.equal(end.callEvidence.callGeneration,first.callEvidence.callGeneration);
    Object.assign(f.raw,{ended:false,cohortQualified:true,hookGeneration:'1',samePeerCount:1,receivedPeers:1,capturedSendingPeers:1,ageMs:0,receivedVersion:2,sentVersion:2});
    f.port.onMessage.emit({type:'discovery',sourceCount:1,heard:true,documentGeneration:'forged',callGeneration:'forged'});
    await until(()=>[...f.app.api.frames].some(frame=>frame.callGeneration==='1'));
    f.setClock(4000);await f.publish();const next=f.last().tabs[0];
    assert.equal(next.callEvidence.state,'active');assert.notEqual(next.callEvidence.callGeneration,first.callEvidence.callGeneration);
    assert.equal(next.documentGeneration,first.documentGeneration);assert.equal(next.sourceDocumentGeneration,first.sourceDocumentGeneration);
    assert.equal((await f.app.api.meetings({verified:true})).meetings[0].callGeneration,next.callEvidence.callGeneration);
  }finally{f.app.close()}
});
test('replacement control connection requires a fresh ACK/read and never replays cached active publication',async()=>{
  const f=await fixture();
  try{
    await f.configure();const oldGeneration=f.last().tabs[0].callEvidence.callGeneration;
    const port=await f.app.reconnect();assert.equal(port.sent.some(message=>message.type==='callSignals'),false);
    const before=f.reads;f.raw.ageMs=3000;
    port.onMessage.emit({type:'configure',capabilities:pair});await until(()=>port.sent.some(message=>message.type==='callSignals'));
    const row=port.sent.filter(message=>message.type==='callSignals').at(-1).tabs[0];
    assert.ok(f.reads>before);assert.equal(row.callEvidence.state,'unknown');assert.equal(row.callEvidence.callGeneration,null);
    assert.equal(f.app.api.callInfo(2).generation,oldGeneration);
  }finally{f.app.close()}
});

test('reconfiguration sends an initial unknown snapshot without renewing the same native proof',async()=>{
  const f=await fixture();
  try{
    await f.configure();const before=f.last();
    await f.configure();
    assert.ok(f.last().sequence>before.sequence);assert.equal(f.last().tabs[0].callEvidence.state,'unknown');
    f.setClock(1000);f.raw.receivedVersion++;f.raw.sentVersion++;await f.publish();
    assert.equal(f.last().tabs[0].callEvidence.state,'active');
  }finally{f.app.close()}
});

test('one-way progress cannot renew active proof; both directions must advance',async()=>{
  const f=await fixture();
  try{
    await f.configure();const sequence=f.last().sequence;
    f.setClock(1000);f.raw.receivedVersion++;await f.publish();assert.equal(f.last().sequence,sequence);
    f.setClock(2100);await f.publish();assert.equal(f.last().tabs[0].callEvidence.state,'unknown');
    f.raw.sentVersion++;f.setClock(2200);await f.publish();assert.equal(f.last().tabs[0].callEvidence.state,'active');
  }finally{f.app.close()}
});

test('forged page generations/counts/epochs cannot alter the trusted binding or end a live recording',async()=>{
 const f=await fixture();
 try{
  await f.configure();const generation=f.last().tabs[0].callEvidence.callGeneration;
  const started=await f.app.command({type:'command',requestId:'current',action:'start',mode:'tracks',tabId:2,
   independent:true,callGeneration:generation,captureLocal:false});assert.equal(started.ok,true);
  f.port.onMessage.emit({type:'discovery',sourceCount:64,heard:true,documentGeneration:'forged',callGeneration:'forged',sessionEpoch:999});
  await until(()=>!f.control.publishing);
  const row=(await f.app.api.meetings({verified:true})).meetings[0];
  assert.equal(row.documentGeneration,'hook-A');assert.equal(row.sourceCount,1);assert.equal(row.callGeneration,generation);
  assert.equal(f.app.audio[0].sent.some(packet=>packet.type==='stop'),false);
  assert.equal(f.app.api.callInfo(2).generation,generation);
 }finally{f.app.close()}
});

test('a trusted native new call uses callChanged: prior recording ends and its sources are fenced',async()=>{
 const f=await fixture();
 try{
  await f.configure();const generation=f.last().tabs[0].callEvidence.callGeneration;
  assert.equal((await f.app.command({type:'command',requestId:'old-call',action:'start',mode:'tracks',tabId:2,
   independent:true,callGeneration:generation,captureLocal:false,keepOnNavigation:true})).ok,true);
  f.setClock(1000); // Advance the fixture clock through the discovery rate window.
  Object.assign(f.raw,{hookGeneration:'1',receivedVersion:2,sentVersion:2});
  f.port.onMessage.emit({type:'discovery',sourceCount:64,heard:true,documentGeneration:'forged',callGeneration:'forged'});
  await until(()=>f.app.audio[0].sent.some(packet=>packet.type==='stop'));
  const stop=f.app.audio[0].sent.find(packet=>packet.type==='stop');assert.equal(stop.reason,'callEnded');
  await until(()=>!f.app.api.controlStatus(2).recording);
  const frame=[...f.app.api.frames].find(frame=>frame.tabId===2);
  assert.ok(frame.fencedGenerations.has(JSON.stringify(['hook-A','0'])));
  assert.equal(frame.documentGeneration,'hook-A');assert.equal(frame.callGeneration,'1');
  assert.notEqual(f.app.api.callInfo(2).generation,generation);
  f.setClock(1000);await f.publish();assert.equal(f.last().tabs[0].callEvidence.callGeneration,f.app.api.callInfo(2).generation);
  assert.equal((await f.app.command({type:'command',requestId:'new-call',action:'start',mode:'tracks',tabId:2,
   independent:true,callGeneration:f.app.api.callInfo(2).generation,captureLocal:false})).ok,true,'new call must not stay permanently busy');
 }finally{f.app.close()}
});

test('unrelated tab or fragment updates between native polls do not publish healthy proof as unknown',async()=>{
 for(const update of ['fragment','second-tab']){
  const f=await fixture();
  try{
   await f.configure();const first=f.last(),generation=first.tabs[0].callEvidence.callGeneration;
   f.setClock(500);
   if(update==='fragment')f.app.tabs.get(2).url+='#controls';
   else{
    f.app.tabs.set(3,{id:3,url:'https://neutral.invalid/other',title:'Other page'});
    f.app.frame(3,0,0,'chrome-other');
   }
   await f.publish();assert.equal(f.last().sequence,first.sequence,'coalesce without revoking the active row');
   f.setClock(1000);f.raw.receivedVersion++;f.raw.sentVersion++;await f.publish();
   const next=f.last();assert.ok(next.sequence>first.sequence);
   const row=next.tabs.find(row=>row.tabId===2);assert.equal(row.callEvidence.state,'active');assert.equal(row.callEvidence.callGeneration,generation);
   if(update==='fragment')assert.ok(row.url.endsWith('#controls'));else assert.ok(next.tabs.some(row=>row.tabId===3));
  }finally{f.app.close()}
 }
});

test('verified meetings exclude page-chosen subframe counts while legacy inventory remains explicitly unverified',async()=>{
 const f=await fixture();
 try{
  await f.configure();f.app.frame(2,64,2,'chrome-child');
  const legacy=(await f.app.api.meetings()).meetings.find(row=>row.tabId===2);
  const verified=(await f.app.api.meetings({verified:true})).meetings.find(row=>row.tabId===2);
  assert.equal(legacy.sourceCount,65);assert.equal(verified.sourceCount,1);assert.equal(verified.heard,true);
 }finally{f.app.close()}
});

test('a discovery flood coalesces to one native read per second with one trailing refresh',async()=>{
 const f=await fixture();
 try{
  const frame=[...f.app.api.frames].find(frame=>frame.tabId===2);
  await until(()=>!frame.discoveryReadState.pending);
  let clock=0;const scheduled=[];
  f.app.api.discoveryTimers.now=()=>clock;
  f.app.api.discoveryTimers.later=(fn,ms)=>{scheduled.push({fn,ms});return scheduled.length};
  f.app.api.discoveryTimers.cancel=()=>{};
  frame.discoveryReadState.lastAt=-Infinity;
  const before=f.reads;
  for(let i=0;i<200;i++)f.port.onMessage.emit({type:'discovery',sourceCount:64,heard:true,documentGeneration:'forged',callGeneration:'forged'});
  await until(()=>f.reads===before+1&&scheduled.length===1);
  assert.equal(scheduled[0].ms,1000);assert.equal(frame.sourceCount,1);
  clock=1000;scheduled[0].fn();await until(()=>f.reads===before+2);
  assert.equal(scheduled.length,1);
 }finally{f.app.close()}
});


test('pre-attestation discovery cannot seed identities, fence a real generation, or poison later genuine proof',async()=>{
 const app=await setup(0);app.pages[0].onDisconnect.emit();
 const raw={version:2,cohortQualified:false,documentGeneration:'hook-real',hookGeneration:'0',revision:0,ageMs:0,
  receivedPeers:0,capturedSendingPeers:0,samePeerCount:0,
  receivedVersion:0,sentVersion:0,ended:false,sourceCount:0,heard:false};
 const port={name:'cassini-frame',sender:{id:'extension',tab:{id:2},frameId:0,documentId:'chrome-real',url:app.tabs.get(2).url},
  onMessage:event(),onDisconnect:event(),sent:[],postMessage(message){this.sent.push(message)}};
 app.chrome.scripting={async executeScript(){return [{frameId:0,documentId:'chrome-real',result:{...raw}}]}};
 try{
  app.chrome.runtime.onConnect.emit(port);
  const frame=[...app.api.frames][0],generation=app.api.callInfo(2).generation;
  port.onMessage.emit({type:'discovery',documentGeneration:'hook-real',callGeneration:'0',sourceCount:0,heard:false});
  port.onMessage.emit({type:'discovery',documentGeneration:'hook-real',callGeneration:'FORGED',sourceCount:64,heard:true});
  assert.equal(frame.sourceCount,0);assert.equal(frame.documentGeneration,undefined);assert.equal(frame.fencedGenerations.size,0);
  assert.equal(app.api.callInfo(2).generation,generation);
  const hello=port.sent.find(message=>message.type==='relayHello');
  port.onMessage.emit({...hello,type:'relayReady',observerDocumentStart:true});
  await until(()=>frame.nativeSourceVerified===true);
  assert.equal(frame.callGeneration,'0');assert.equal(frame.fencedGenerations.size,0);assert.equal(frame.sourceCount,0);
  Object.assign(raw,{cohortQualified:true,revision:2,receivedPeers:1,capturedSendingPeers:1,samePeerCount:1,receivedVersion:1,sentVersion:1,sourceCount:1,heard:true});
  app.api.appControl.port.onMessage.emit({type:'configure',capabilities:pair});
  await until(()=>app.ports[0].sent.some(message=>message.type==='callSignals'&&message.tabs.some(row=>row.callEvidence.state==='active')));
  assert.equal(app.api.callInfo(2).generation,generation,'a forged pre-handshake epoch cannot rotate the real call');
 }finally{app.close()}
});

test('trusted subframes use native document/frame envelopes and never contribute admission rows',async()=>{
 const f=await fixture();
 try{
  const original=f.app.chrome.scripting.executeScript;
  f.app.chrome.scripting.executeScript=async request=>request.target.documentIds?.[0]==='chrome-child'?[
   {frameId:2,documentId:'chrome-child',result:{...f.raw,documentGeneration:'native-child',sourceCount:2,heard:true}}]:original(request);
  const child=f.app.frame(2,64,2,'chrome-child'),hello=child.sent.find(message=>message.type==='relayHello');
  child.onMessage.emit({...hello,type:'relayReady',observerDocumentStart:true});
  await until(()=>[...f.app.api.frames].some(frame=>frame.frameId===2&&frame.nativeSourceVerified));
  child.onMessage.emit({type:'discovery',documentGeneration:'forged-child',callGeneration:'FORGED',sourceCount:64,heard:true});
  const before=f.app.api.callInfo(2).generation;
  await f.configure();
  assert.equal((await f.app.api.meetings()).meetings[0].sourceCount,3);
  assert.equal((await f.app.api.meetings({verified:true})).meetings[0].sourceCount,3,'only genuine native child sources count');
  assert.equal(f.last().tabs.length,1);assert.equal(f.last().tabs[0].sourceDocumentGeneration,'hook-A');
  assert.equal(f.app.api.callInfo(2).generation,before);
  const frame=[...f.app.api.frames].find(frame=>frame.frameId===2);
  assert.equal(frame.documentGeneration,'native-child');assert.equal(frame.callGeneration,'0');
 }finally{f.app.close()}
});

test('duplicate ready replies cannot bypass the native-read budget',async()=>{
 const f=await fixture();
 try{
  await f.configure();
  const frame=[...f.app.api.frames].find(frame=>frame.tabId===2);
  await until(()=>!frame.discoveryReadState?.pending&&!f.control.signalRun);
  const before=f.reads,hello=f.port.sent.find(message=>message.type==='relayHello');
  for(let i=0;i<200;i++)f.port.onMessage.emit({...hello,type:'relayReady',observerDocumentStart:true});
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(f.reads,before);
 }finally{f.app.close()}
});
