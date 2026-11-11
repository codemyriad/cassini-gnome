// Model browser-owned executeScript results; real media is tested separately.
const {test}=require('node:test');const assert=require('node:assert/strict');
const vm=require('node:vm'),fs=require('node:fs'),path=require('node:path');
const {until}=require('./support/app-control-harness.cjs');
const context=vm.createContext({URL,TextEncoder,setTimeout,clearTimeout});
vm.runInContext(fs.readFileSync(path.join(__dirname,'../call-signals.js'),'utf8').replace(/^export /gm,'')+'\nglobalThis.Generic=GenericCallEvidenceProducer;',context);
const Generic=context.Generic;
function fixture(){
  const frame={tabId:2,frameId:0,browserUrl:'https://neutral.invalid/call',browserDocumentId:'chrome-A',
    documentGeneration:'hook-A',callGeneration:'hook-session-A',obsoleteEpoch:0,observerDocumentStart:true,sourceCount:1,heard:true};
  const sample={version:2,cohortQualified:true,documentGeneration:'hook-A',hookGeneration:'hook-session-A',revision:2,ageMs:10,
    receivedPeers:1,capturedSendingPeers:1,samePeerCount:1,receivedVersion:1,sentVersion:1,ended:false,sourceCount:1,heard:true};
  let live=true,identity='background-A',reads=0;
  const producer=new Generic({identity:()=> 'profile-A',tabs:{async query(){return [{id:2,url:frame.browserUrl}]}},
    frames:()=>[frame],current:value=>value===frame&&live,callIdentity:()=>identity,
    async executeScript(request){reads++;assert.equal(request.world,'MAIN');assert.deepEqual([...request.target.documentIds],['chrome-A']);
      return [{frameId:frame.frameId,documentId:'chrome-A',result:{...sample}}]}});
  return {frame,sample,producer,get reads(){return reads},retire(){live=false},rotate(){identity='background-B'}};
}
test('only acknowledged generic snapshots expose unequal mapped IDs and the background-owned generation',async()=>{
  const f=fixture();assert.equal((await f.producer.snapshot()).tabs.length,0);assert.equal(f.reads,0);
  const row=(await f.producer.snapshot({verified:true})).tabs[0];
  assert.equal(row.documentGeneration,'chrome-A');assert.equal(row.sourceDocumentGeneration,'hook-A');
  assert.equal(row.callEvidence.callGeneration,'background-A');assert.equal(row.callEvidence.state,'active');
  assert.equal(row.remoteAudio,'separated');assert.notEqual(row.documentGeneration,row.sourceDocumentGeneration);
});
test('passive, sender-only, stale and malformed observations remain unknown while real source facts are untouched',async()=>{
  for(const [index,mutation] of [s=>{s.cohortQualified=false;s.samePeerCount=0;s.capturedSendingPeers=0},
    s=>{s.cohortQualified=false;s.samePeerCount=0;s.receivedPeers=0},s=>{s.ageMs=2501},
    s=>{s.documentGeneration='chrome-A'},s=>{s.hookGeneration='retired-hook-session'},
    s=>{s.samePeerCount=2},s=>{s.revision=0},s=>{s.ageMs=NaN},s=>{s.version=1},s=>{s.cohortQualified='true'},s=>{s.capturedSendingPeers=0}].entries()){
    const f=fixture();mutation(f.sample);
    const row=(await f.producer.snapshot({verified:true})).tabs[0];
    assert.equal(row.callEvidence.state,'unknown');assert.equal(row.callEvidence.callGeneration,null);
    assert.equal(f.frame.sourceCount,1);assert.equal(f.frame.heard,true);
    assert.equal(row.remoteAudio,index<2?'separated':'unknown');
  }
});
test('D80.5 split PCs in one frame qualify without a same-PC or backend identity match',async()=>{
  const f=fixture();Object.assign(f.sample,{samePeerCount:0,receivedPeers:2,sourceCount:2});
  const row=(await f.producer.snapshot({verified:true})).tabs[0];
  assert.equal(row.callEvidence.state,'active');assert.equal(row.remoteAudio,'separated');
  assert.equal(row.callEvidence.adapter.version,'2');
});
test('multiple duplex PCs still form one document cohort',async()=>{
  const f=fixture();Object.assign(f.sample,{samePeerCount:2,receivedPeers:2,capturedSendingPeers:2});
  assert.equal((await f.producer.snapshot({verified:true})).tabs[0].callEvidence.state,'active');
});
test('a receiver in another frame prevents claiming a scoped separated capture route',async()=>{
  const f=fixture(),child={...f.frame,frameId:2,browserDocumentId:'chrome-child',nativeSourceVerified:true,sourceCount:1};
  f.producer.frames=()=>[f.frame,child];f.producer.currentSource=value=>value===f.frame||value===child;
  const row=(await f.producer.snapshot({verified:true})).tabs[0];
  assert.equal(row.callEvidence.state,'active');assert.equal(row.remoteAudio,'unknown');
});
test('sender-only child facts cannot complete a receiving-only top-frame cohort',async()=>{
  const f=fixture(),child={...f.frame,frameId:2,browserDocumentId:'chrome-child',nativeSourceVerified:true,sourceCount:0};
  Object.assign(f.sample,{cohortQualified:false,samePeerCount:0,capturedSendingPeers:0,sentVersion:0});
  f.producer.frames=()=>[f.frame,child];f.producer.currentSource=value=>value===f.frame||value===child;
  assert.equal((await f.producer.snapshot({verified:true})).tabs[0].callEvidence.state,'unknown');
});
test('receiving and sending observations in separate documents are never pooled',async()=>{
  const f=fixture(),other={...f.frame,tabId:3,browserDocumentId:'chrome-B',documentGeneration:'hook-B'};
  Object.assign(f.sample,{cohortQualified:false,samePeerCount:0,capturedSendingPeers:0,sentVersion:0});
  f.producer.frames=()=>[f.frame,other];f.producer.current=value=>value===f.frame||value===other;
  f.producer.currentSource=f.producer.current;
  f.producer.tabs.query=async()=>[f.frame,other].map(frame=>({id:frame.tabId,url:frame.browserUrl}));
  f.producer.executeScript=async request=>{const second=request.target.documentIds[0]==='chrome-B';return [{frameId:0,
    documentId:second?'chrome-B':'chrome-A',result:second?{...f.sample,documentGeneration:'hook-B',receivedPeers:0,capturedSendingPeers:1,receivedVersion:0,sentVersion:1}:f.sample}]};
  const result=await f.producer.snapshot({verified:true});assert.equal(result.tabs.length,2);
  assert.ok(result.tabs.every(row=>row.callEvidence.state==='unknown'));
});
test('fenced port, Chrome/hook identity and background-generation changes cannot complete an old read',async()=>{
  for(const mutate of [f=>f.retire(),f=>{f.frame.browserDocumentId='chrome-B'},f=>{f.frame.documentGeneration='hook-B'},
    f=>{f.frame.callGeneration='hook-session-B'},f=>{f.frame.obsoleteEpoch++},f=>f.rotate()]){
    const f=fixture();let release;
    f.producer.executeScript=()=>new Promise(resolve=>release=resolve);
    const pending=f.producer.snapshot({verified:true});
    await until(()=>release);
    mutate(f);release([{frameId:0,documentId:'chrome-A',result:f.sample}]);
    assert.equal((await pending).tabs.length,0);
  }
});
test('a hung browser read withdraws positive evidence at a bounded deadline',async()=>{
  const f=fixture();let expire;
  f.producer.executeScript=()=>new Promise(()=>{});
  f.producer.timers={later(fn){expire=fn;return 1},cancel(){}};
  const pending=f.producer.snapshot({verified:true});
  // The query completes first; wait on execution of the read, not elapsed time.
  let entered=false;f.producer.executeScript=()=>{entered=true;return new Promise(()=>{})};
  await until(()=>entered);
  expire();const row=(await pending).tabs[0];
  assert.equal(row.callEvidence.state,'unknown');assert.equal(row.callEvidence.callGeneration,null);
});

test('browser round-trip time consumes proof freshness instead of refreshing old media',async()=>{
  const f=fixture();let clock=0;f.producer.now=()=>clock;
  f.sample.ageMs=2000;
  f.producer.executeScript=async()=>{clock=600;return [{frameId:0,documentId:'chrome-A',result:f.sample}]};
  const row=(await f.producer.snapshot({verified:true})).tabs[0];
  assert.equal(row.callEvidence.state,'unknown');assert.equal(row.callEvidence.callGeneration,null);
});

test('a superseded configuration cannot consume the new initial snapshot via the dedup cache',async()=>{
  const f=fixture(),queries=[];
  Object.assign(f.sample,{cohortQualified:false,samePeerCount:0,capturedSendingPeers:0});
  f.producer.tabs.query=()=>new Promise(resolve=>queries.push(resolve));
  const old=f.producer.snapshot({verified:true,epoch:1,configurationEpoch:1});
  const fresh=f.producer.snapshot({verified:true,epoch:1,configurationEpoch:2});
  await until(()=>queries.length===2);
  queries[0]([{id:2,url:f.frame.browserUrl}]);assert.equal(await old,null);
  queries[1]([{id:2,url:f.frame.browserUrl}]);
  const result=await fresh;assert.equal(result.tabs[0].callEvidence.state,'unknown');
});

test('the final publication check withdraws expired proof or removes a retired document',async()=>{
  const f=fixture();let clock=0;f.producer.now=()=>clock;
  const prepared=await f.producer.snapshot({verified:true});
  clock=2500;const expired=f.producer.finalize(prepared);
  assert.equal(expired.tabs[0].callEvidence.state,'unknown');assert.equal(expired.tabs[0].callEvidence.callGeneration,null);
  f.retire();assert.equal(f.producer.finalize(prepared).tabs.length,0);
});


test('non-HTTP subframes never execute or adopt a native source snapshot',async()=>{
 for(const url of ['about:blank','about:srcdoc','blob:https://neutral.invalid/generated','data:text/html,generated','invalid URL']){
  const f=fixture();Object.assign(f.frame,{frameId:2,browserUrl:url,nativeSourceVerified:true,nativeCollectorUnavailable:true});
  let adopted=0;f.producer.onNativeRead=()=>adopted++;
  assert.equal(await f.producer.read(f.frame),null,url);assert.equal(f.reads,0,url);assert.equal(adopted,0,url);
  assert.equal(f.frame.nativeSourceVerified,false);assert.equal(f.frame.nativeCollectorUnavailable,false);
 }
});
test('HTTP and HTTPS subframes retain authenticated native source observation',async()=>{
 for(const url of ['http://neutral.invalid/child','https://neutral.invalid/child']){
  const f=fixture();Object.assign(f.frame,{frameId:2,browserUrl:url});
  let adopted=0;f.producer.onNativeRead=()=>adopted++;
  assert.ok(await f.producer.read(f.frame));assert.equal(f.reads,1);assert.equal(adopted,1);
 }
});
