const {test}=require('node:test');const assert=require('node:assert/strict');
const vm=require('node:vm'),fs=require('node:fs'),path=require('node:path');
const {setup,until}=require('./support/app-control-harness.cjs');
const context=vm.createContext({URL,TextEncoder,setTimeout,clearTimeout});
vm.runInContext(fs.readFileSync(path.join(__dirname,'../call-signals.js'),'utf8').replace(/^export /gm,'')+'\nglobalThis.signalApi={browserProfileIdentity,SiteSignalAdapters,CallSignalProducer};',context);
const {browserProfileIdentity,SiteSignalAdapters,CallSignalProducer}=context.signalApi;
const plain=value=>JSON.parse(JSON.stringify(value));
const configure=app=>app.api.appControl.port.onMessage.emit({type:'configure',capabilities:['call-signals-v1']});
async function fixtureFrame(app,count=0){
 app.tabs.get(2).url='https://fixture.example/';
 app.pages[0].onDisconnect.emit();
 return app.frame(2,count,0,'browser-document-A');
}

test('profile identity persists across independent workers and never uses connection identity',async()=>{
 const saved={};const storage={async get(){return {...saved}},async set(value){Object.assign(saved,value)}};
 assert.equal(await browserProfileIdentity(storage,()=> 'stable-profile'), 'stable-profile');
 assert.equal(await browserProfileIdentity(storage,()=> {throw Error('must not remint')}), 'stable-profile');
 assert.equal(saved.cassiniBrowserInstance,'stable-profile');
});
test('profile storage failure, failed persistence and stalled lookup fail closed without a random fallback',async()=>{
 assert.equal(await browserProfileIdentity(undefined,()=> 'ephemeral'),null);
 assert.equal(await browserProfileIdentity({async get(){return {}},async set(){}},()=> 'not-persisted'),null);
 let expire,cancelled=false;
 const pending=browserProfileIdentity({get(){return new Promise(()=>{})}},()=> 'not-used',{later(fn,ms){assert.equal(ms,2500);expire=fn;return 1},cancel(){cancelled=true}});
 expire();assert.equal(await pending,null);assert.equal(cancelled,true);
});
test('production descriptors contain no hostname registry; explicit legacy fixtures never infer a call',()=>{
 const adapters=new SiteSignalAdapters();
 for(const address of ['https://fixture.example/','https://fixture.example/#status','https://www.youtube.com/'])assert.equal(adapters.resolve(address),null);
 const generic=new SiteSignalAdapters([{name:'review-fixture',version:'1',hostname:'fixture.example',remoteAudio:'separated'}]);
 assert.equal(generic.resolve('https://fixture.example/').remoteAudio,'separated');
 assert.equal(generic.evidence(generic.resolve('https://fixture.example/')).state,'unknown');
});
test('producer uses Chrome top document and URL, excludes obsolete/subframes and ignores page evidence',async()=>{
 const adapters=new SiteSignalAdapters([{name:'fixture',version:'1',hostname:'fixture.example',remoteAudio:'mixed'}]),frame={tabId:2,frameId:0,browserDocumentId:'browser-A',browserUrl:'https://fixture.example/',sourceCount:30,heard:true,callEvidence:{state:'active'}};
 let valid=true;const frames=new Set([frame,{...frame,frameId:1,browserDocumentId:'subframe'}]);
 const producer=new CallSignalProducer({identity:()=> 'profile',tabs:{async query(){return [{id:2,url:'https://fixture.example/'}]}},frames:()=>frames,current:f=>f===frame&&valid,adapters});
 const snapshot=await producer.snapshot();assert.equal(snapshot.tabs.length,1);assert.equal(snapshot.tabs[0].documentGeneration,'browser-A');assert.equal(snapshot.tabs[0].callEvidence.state,'unknown');
 valid=false;assert.equal((await producer.snapshot()).tabs.length,0);
 valid=true;frame.browserUrl='https://other.example/';assert.equal((await producer.snapshot()).tabs.length,0);
});
test('a held browser lookup cannot publish a retired/replaced document',async()=>{
 const frame={tabId:2,frameId:0,browserDocumentId:'old',browserUrl:'https://fixture.example/'};
 let release,current=true;const producer=new CallSignalProducer({identity:()=> 'profile',tabs:{query(){return new Promise(resolve=>release=resolve)}},frames:()=>[frame],current:()=>current});
 const pending=producer.snapshot();current=false;release([{id:2,url:'https://fixture.example/'}]);assert.equal((await pending).tabs.length,0);
});
test('signals require opt-in, acknowledge first, have stable identity and withdraw on navigation',async()=>{
 const app=await setup(0);
 try{
  await fixtureFrame(app);
  await app.api.appControl.publishing;
  assert.ok(app.ports[0].sent[0].capabilities.includes('call-signals-v1'));
  assert.equal(app.ports[0].sent.some(m=>m.type==='callSignals'),false);
  configure(app);await until(()=>app.ports[0].sent.some(m=>m.type==='callSignals'));
  const messages=app.ports[0].sent,ack=messages.findIndex(m=>m.type==='configured'),first=messages.findIndex(m=>m.type==='callSignals');assert.ok(ack<first);
  const signal=messages[first];assert.equal(signal.browserInstance,app.localStore.cassiniBrowserInstance);assert.equal(signal.sequence,0);assert.equal(signal.tabs.length,0,'v1-only peers receive no generic verified rows');
  app.chrome.tabs.onUpdated.emit(2,{status:'loading'});
  await until(()=>messages.filter(m=>m.type==='callSignals').at(-1).tabs.length===0);
  const withdrawn=messages.filter(m=>m.type==='callSignals').at(-1);assert.ok(withdrawn.sequence>=signal.sequence);
  assert.equal(app.audio.length,0);
 }finally{app.close()}
});
test('revocation drops a held snapshot, and a replacement connection does not await the stale producer',async()=>{
 const app=await setup(0);let release;
 try{
  const control=app.api.appControl,port=control.port;
  control.signalSnapshot=()=>new Promise(resolve=>release=resolve);configure(app);await until(()=>release);
  port.onMessage.emit({type:'configure',capabilities:[]});
  release({browserInstance:'profile',tabs:[]});await new Promise(resolve=>setImmediate(resolve));
  assert.equal(port.sent.some(m=>m.type==='callSignals'),false);
  let oldRelease;control.signalSnapshot=()=>new Promise(resolve=>oldRelease=resolve);configure(app);await until(()=>oldRelease);
  const next=await app.reconnect();control.signalSnapshot=async()=>({browserInstance:'profile',tabs:[]});
  assert.equal(next.sent.some(m=>m.type==='callSignals'),false,'reconnect is not opt-in');
  configure(app);await until(()=>next.sent.some(m=>m.type==='callSignals'));
  assert.equal(next.sent.find(m=>m.type==='callSignals').sequence,0);
  oldRelease({browserInstance:'stale-profile',tabs:[]});await new Promise(resolve=>setImmediate(resolve));
  assert.equal(next.sent.filter(m=>m.type==='callSignals').length,1);
 }finally{app.close()}
});
test('invalid configure and unverified profile storage never enable signals',async()=>{
 const app=await setup(0,{},[],null,'no-profile-',null,{localStorage:{async get(){throw Error('storage failed')}}});
 try{
  assert.equal(app.ports[0].sent[0].capabilities.includes('call-signals-v1'),false);
  configure(app);assert.deepEqual(plain(app.ports[0].sent.at(-1)),{type:'configured',capabilities:[]});
  app.ports[0].onMessage.emit({type:'configure',capabilities:['call-signals-v1'],pageManaged:true});
  assert.equal(app.ports[0].sent.some(m=>m.type==='callSignals'),false);assert.equal(app.audio.length,0);
 }finally{app.close()}
});
test('hostname never grants admission or blocks explicit capture of real RTP sources',async()=>{
 const app=await setup(0);
 try{
  const frame=await fixtureFrame(app,2);
  frame.onMessage.emit({type:'discovery',sourceCount:2,heard:true,documentGeneration:'page-spoof',callGeneration:'page-spoof'});
  assert.equal((await app.api.meetings()).meetings.length,1);
  app.api.appControl.port.onMessage.emit({type:'configure',capabilities:['call-signals-v1','verified-auto-admission-v1']});
  await until(()=>app.ports[0].sent.some(m=>m.type==='callSignals'&&m.tabs.length));
  assert.equal(app.ports[0].sent.filter(m=>m.type==='callSignals').at(-1).tabs[0].callEvidence.state,'unknown');
  const started=await app.command({type:'command',requestId:'manual',action:'start',mode:'tracks',tabId:2,independent:true,microphone:false});
  assert.equal(started.ok,true);assert.equal(app.audio.length,1);await app.api.stop();
 }finally{app.close()}
});
test('publisher deduplicates unchanged snapshots without resetting sequence on reconfigure',async()=>{
 const app=await setup(0);
 try{
  configure(app);await until(()=>app.ports[0].sent.some(m=>m.type==='callSignals'));
  app.api.appControl.publishSignals();await new Promise(resolve=>setImmediate(resolve));
  assert.equal(app.ports[0].sent.filter(m=>m.type==='callSignals').length,1);
  configure(app);await until(()=>app.ports[0].sent.filter(m=>m.type==='callSignals').length===2);
  assert.deepEqual(app.ports[0].sent.filter(m=>m.type==='callSignals').map(m=>m.sequence),[0,1]);
 }finally{app.close()}
});

test('trusted adapter framework produces literal lifecycle transitions, with document/generation fencing',async()=>{
 const root=path.resolve(__dirname,'../..');
 const contract=fs.existsSync(path.join(root,'conformance/control/call-signals-vectors.json'))?root:path.join(root,'browser-protocol');
 const fixture=JSON.parse(fs.readFileSync(path.join(contract,'conformance/control/call-signals-vectors.json'))).lifecycle;
 const frame={tabId:2,frameId:0,browserDocumentId:'browser-A',browserUrl:'https://fixture.example/'};
 let facts;
 const adapters=new SiteSignalAdapters([{name:'fixture-only',version:'1',hostname:'fixture.example',remoteAudio:'mixed',evidence:()=>facts}]);
 const producer=new CallSignalProducer({identity:()=> 'profile',tabs:{async query(){return [{id:2,url:'https://fixture.example/'}]}},frames:()=>[frame],current:()=>true,adapters});
 for(const step of fixture.steps){
  const expected=step.signal.tabs[0].callEvidence;
  facts={documentGeneration:'browser-A',state:expected.state,reason:expected.reason,callGeneration:expected.callGeneration};
  const row=(await producer.snapshot()).tabs[0];
  assert.equal(row.callEvidence.state,expected.state);assert.equal(row.callEvidence.callGeneration,expected.callGeneration);
 }
 facts={documentGeneration:'superseded',state:'active',reason:'activeCall',callGeneration:'old-call'};
 const fenced=(await producer.snapshot()).tabs[0];assert.equal(fenced.callEvidence.state,'unknown');assert.equal(fenced.callEvidence.reason,'evidenceConflict');
 facts={documentGeneration:'browser-A',state:'active',reason:'activeCall'};
 assert.equal((await producer.snapshot()).tabs[0].callEvidence.state,'unknown');
 facts={documentGeneration:'browser-A',state:'inactive',reason:'unverified',callGeneration:null};
 assert.equal((await producer.snapshot()).tabs[0].callEvidence.state,'unknown');
});

test('stalled browser inventory withdraws facts at its bounded deadline, without waiting for audio',async()=>{
 let expire;
 const producer=new CallSignalProducer({identity:()=> 'profile',tabs:{query(){return new Promise(()=>{})}},frames:()=>[],current:()=>false,
  timers:{later(fn,ms){assert.equal(ms,2500);expire=fn;return 1},cancel(){}}});
 const pending=producer.snapshot();expire();assert.deepEqual(plain(await pending),{browserInstance:'profile',tabs:[]});
});
