const {test}=require('node:test');
const assert=require('node:assert/strict');
const {setup,until}=require('./support/app-control-harness.cjs');
const command=(requestId,action,extra={})=>({type:'command',requestId,action,tabId:2,mode:'tracks',independent:true,...extra});
async function packet(app,value,id){
 const page=app.pages[0];page.onMessage.emit({type:'packet',request:id,packet:value});
 await until(()=>page.sent.some(m=>m.type==='ack'&&m.request===id));
 return page.sent.find(m=>m.type==='ack'&&m.request===id);
}
test('outcome packet types are enabled only by native host negotiation',async()=>{
 for(const enabled of [false,true]){
  const app=await setup(3,{},enabled?['source-outcomes-v1']:[]);
  try {
   await app.command(command('start','start'));
   assert.deepEqual(Array.from(app.audio[0].sent[0].features),['source-outcomes-v1']);
   assert.equal(app.pages[0].sent.find(m=>m.type==='start').sourceOutcomes,enabled);
   if(enabled){await packet(app,{type:'track',id:'span',kind:'remote',sampleRate:48000,channels:1},0);assert.equal((await packet(app,{type:'trackEnd',id:'span',reason:'ended',endOffsetMs:1000,droppedFrames:0},1)).error,undefined)}
   else {app.pages[0].onMessage.emit({type:'packet',request:'legacy-source',packet:{type:'sources',discoveredSources:[{sourceTrackId:'ignored',kind:'remote'}]}});assert.equal(app.audio[0].sent.some(p=>p.type==='sources'),false)}
   await app.command(command('stop','stop'));
   const stopped=app.audio[0].sent.find(m=>m.type==='stop');assert.equal(stopped.captureId,app.audio[0].sent[0].captureId);
   assert.equal(Array.isArray(stopped.discoveredSources),enabled);
  } finally{app.close()}
 }
});
test('a per-source native refusal is acknowledged without closing the recording',async()=>{
 const app=await setup(3,{},['source-outcomes-v1']);
 try {
  await app.command(command('start','start'));const port=app.audio[0],post=port.postMessage;
  port.postMessage=function(value){if(value.id==='refused'){this.sent.push(value);queueMicrotask(()=>this.onMessage.emit({type:'error',code:'source',scope:'source',id:value.id,message:'Generated refusal'}))}else post.call(this,value)};
  const refused=await packet(app,{type:'track',id:'refused',sourceTrackId:'missing-voice',kind:'remote',sampleRate:48000,channels:1},1);
  assert.equal(refused.scope,'source');assert.equal(port.closed,false);
  await packet(app,{type:'track',id:'healthy',kind:'remote',sampleRate:48000,channels:1},0);
  assert.equal((await packet(app,{type:'audio',id:'healthy',sequence:0,offsetMs:0,pcm:'AAA='},2)).error,undefined);
  assert.equal(app.api.controlStatus().sessions.length,1);
  assert.deepEqual(Array.from(app.api.controlStatus().sessions[0].failedSources),['missing-voice']);
  assert.deepEqual(Array.from(app.api.controlStatus().failedSources),['missing-voice']);
  await app.command(command('stop','stop'));
 }finally{app.close()}
});
test('a stale capture stop does not cancel a newer pending start',async()=>{
 const app=await setup();let release;
 try {
  const get=app.chrome.tabs.get;let entered=false;const gate=new Promise(resolve=>release=resolve);
  app.chrome.tabs.get=async id=>{if(id===2){entered=true;await gate}return get(id)};
  const starting=app.api.start('tracks',2,{independent:true});await until(()=>entered);
  assert.equal((await app.command(command('wrong-stop','stop',{captureId:'old-token'}))).ok,false);
  release();await starting;assert.equal(app.audio.length,1);assert.equal(app.audio[0].closed,false);
  const current=app.api.controlStatus().sessions[0].captureId;
  assert.equal((await app.command(command('current-stop','stop',{captureId:current}))).ok,true);
 }finally{release?.();app.close()}
});
test('a metadata lookup hiccup preserves the active row and marks its count unknown',async()=>{
 const app=await setup();
 try {
  await app.command(command('start','start'));let rows=await app.api.meetings();assert.equal(rows.meetings[0].sourceCount,3);
  const get=app.chrome.tabs.get;app.chrome.tabs.get=async id=>{if(id===2)throw Error('generated tab metadata hiccup');return get(id)};
  rows=await app.api.meetings();assert.equal(rows.meetings.length,1);assert.equal(rows.meetings[0].tabId,2);
  assert.equal(rows.meetings[0].sourceCount,3);assert.equal(rows.meetings[0].sourceCountKnown,false);
  app.chrome.tabs.get=get;await app.command(command('stop','stop'));
 }finally{app.close()}
});

test('call address change retires A, publishes a new generation, and rejects late A commands',async()=>{
 const app=await setup(3,{},['source-outcomes-v1']);
 try {
  await app.command(command('start-A','start',{keepOnNavigation:true}));
  const old=app.api.controlStatus().sessions[0],port=app.audio[0];
  const page=app.pages[0],post=page.postMessage;let release;
  page.postMessage=function(message){if(message.type==='stop')release=()=>this.onMessage.emit({type:'stopped'});else post.call(this,message)};
  app.chrome.tabs.onUpdated.emit(2,{url:'https://meet.example/call-B'});
  await until(()=>release);assert.equal(app.api.controlStatus().sessions[0].stopping,true);
  const refused=await packet(app,{type:'audio',id:'old-span',sequence:0,offsetMs:0,pcm:'AAA='},1);
  assert.equal(refused.scope,'source');assert.ok(!port.sent.some(p=>p.type==='audio'));
  const lateSource=await packet(app,{type:'sources',discoveredSources:[{sourceTrackId:'late-A',kind:'remote',failed:true}]},2);
  assert.equal(lateSource.code,'callChanged');assert.deepEqual(Array.from(app.api.controlStatus().sessions[0].failedSources),[]);
  release();await until(()=>port.closed);
  const stop=port.sent.find(p=>p.type==='stop');assert.equal(stop.reason,'callEnded');assert.equal(stop.captureId,old.captureId);
  app.tabs.get(2).url='https://meet.example/call-B';app.pages[0].onMessage.emit({type:'discovery',sourceCount:1,heard:true});
  const row=(await app.api.meetings()).meetings[0];assert.notEqual(row.callGeneration,old.callGeneration);
  assert.equal((await app.command(command('stale-A','start',{callGeneration:old.callGeneration}))).ok,false);
  assert.equal(app.audio.length,1);
 }finally{app.close()}
});

test('live source batches retain 257 never-opened sources and Stop waits for their native ACK',async()=>{
 const app=await setup(3,{},['source-outcomes-v1']);let release;
 try{
  await app.command(command('start','start'));const port=app.audio[0],post=port.postMessage;
  port.postMessage=function(value){if(value.type==='sources'){this.sent.push(value);release=()=>this.onMessage.emit({type:'ack'})}else post.call(this,value)};
  const sources=Array.from({length:257},(_,i)=>({sourceTrackId:`never-opened-${i}`,kind:'remote',label:'Guest',failed:true}));
  const update=packet(app,{type:'sources',discoveredSources:sources},257);
  await until(()=>release);
  assert.equal(port.sent.filter(p=>p.type==='track').length,0);
  assert.equal(app.api.controlStatus().sessions[0].failedSources.length,257);
  const stopping=app.command(command('stop','stop'));
  await until(()=>app.pages[0].sent.some(m=>m.type==='stop'));
  assert.equal(port.sent.some(p=>p.type==='stop'),false,'native Stop cannot overtake unacknowledged source records');
  release();await update;await stopping;
  assert.equal(port.sent.find(p=>p.type==='sources').discoveredSources.length,257);
  assert.deepEqual(Array.from(port.sent.find(p=>p.type==='stop').discoveredSources),[]);
 }finally{release?.();app.close()}
});

test('a legacy full discovery update is streamed without the old 64/256 truncation',async()=>{
 const app=await setup(3,{},['source-outcomes-v1']);
 try{
  await app.command(command('start','start'));
  app.pages[0].onMessage.emit({type:'warning',discoveredSources:Array.from({length:600},(_,i)=>({sourceTrackId:`source-${i}`,kind:'remote',label:'🙂'.repeat(200),failed:true}))});
  await until(()=>app.audio[0].sent.filter(p=>p.type==='sources').flatMap(p=>p.discoveredSources).length===600);
  const packets=app.audio[0].sent.filter(p=>p.type==='sources');
  assert.equal(new Set(packets.flatMap(p=>p.discoveredSources).map(s=>s.sourceTrackId)).size,600);
  assert.ok(packets.length>1,'multibyte source labels require more than one native frame');
  assert.ok(packets.every(p=>Buffer.byteLength(JSON.stringify(p))<=400000));
  await app.command(command('stop','stop'));
 }finally{app.close()}
});

test('whole-tab discovery is durable before offscreen setup and setup failure marks that source',async()=>{
 const app=await setup(3,{},['source-outcomes-v1']);
 try{
  app.chrome.tabCapture.getMediaStreamId=async()=>'granted-stream';
  app.chrome.offscreen.createDocument=async()=>{
   assert.equal(app.audio[0].sent.filter(p=>p.type==='sources').length,1,'the tab source is announced before any offscreen work');
   throw Error('generated offscreen setup failure');
  };
  await assert.rejects(app.api.start('tab',2),/generated offscreen setup failure/);
  const updates=app.audio[0].sent.filter(p=>p.type==='sources').flatMap(p=>p.discoveredSources);
  assert.equal(updates.length,2);assert.equal(updates[0].kind,'tab');assert.equal(updates[0].failed,false);assert.equal(updates[1].failed,true);
  assert.equal(updates[0].sourceTrackId,updates[1].sourceTrackId);
  assert.equal(app.audio[0].sent.filter(p=>p.type==='track').length,0);
 }finally{app.close()}
});

test('source batches validate UTF-8 bounds atomically without closing other audio',async()=>{
 const app=await setup(3,{},['source-outcomes-v1']);
 try{
  await app.command(command('start','start'));
  const invalid=await packet(app,{type:'sources',discoveredSources:[{sourceTrackId:'would-be-accepted',kind:'remote'},{sourceTrackId:'é'.repeat(101),kind:'remote'}]},1);
  assert.equal(invalid.scope,'command');assert.equal(app.audio[0].sent.some(p=>p.type==='sources'),false);
  assert.equal((await packet(app,{type:'sources',discoveredSources:[{sourceTrackId:'é'.repeat(100),kind:'remote',failed:false}]},2)).error,undefined);
  await packet(app,{type:'track',id:'unrelated',kind:'remote',sampleRate:48000,channels:1},0);
  assert.equal((await packet(app,{type:'audio',id:'unrelated',sequence:0,offsetMs:0,pcm:'AAA='},3)).error,undefined);
  assert.equal(app.audio[0].closed,false);await app.command(command('stop','stop'));
 }finally{app.close()}
});

test('native-first Stop retires the extension without false failed-source updates',async()=>{
 const app=await setup(3,{},['source-outcomes-v1']);
 try{
  await app.command(command('start','start'));const port=app.audio[0],post=port.postMessage;
  port.postMessage=function(value){if(value.type==='sources'){this.sent.push(value);queueMicrotask(()=>this.onMessage.emit({type:'error',code:'state',scope:'command',message:'Capture already saved'}))}else post.call(this,value)};
  const refused=await packet(app,{type:'sources',discoveredSources:[{sourceTrackId:'late',kind:'remote',failed:false}]},1);
  assert.equal(refused.code,'state');await until(()=>port.closed);
  assert.equal(port.sent.filter(p=>p.type==='sources').length,1);
  assert.equal(port.sent.find(p=>p.type==='sources').discoveredSources[0].failed,false);
 }finally{app.close()}
});

test('two frames cannot concurrently forward a 65th native track and refusal releases its reservation',async()=>{
 const app=await setup(3,{},['source-outcomes-v1']);let release;
 try{
  await app.command(command('start','start'));const second=app.frame(2,1,1),port=app.audio[0],post=port.postMessage;
  for(let i=0;i<63;i++)await packet(app,{type:'track',id:`admitted-${i}`,kind:'remote',sampleRate:48000,channels:1},i);
  port.postMessage=function(value){if(value.type==='track'&&value.id==='pending-64'){this.sent.push(value);release=()=>this.onMessage.emit({type:'error',code:'source',scope:'source',id:value.id,message:'Generated source refusal'})}else post.call(this,value)};
  const pending=packet(app,{type:'track',id:'pending-64',kind:'remote',sampleRate:48000,channels:1},64);
  await until(()=>release);
  second.onMessage.emit({type:'packet',request:65,packet:{type:'track',id:'overflow-65',kind:'remote',sampleRate:48000,channels:1}});
  await until(()=>second.sent.some(m=>m.type==='ack'&&m.request===65));
  assert.equal(second.sent.find(m=>m.type==='ack'&&m.request===65).code,'limit');
  assert.equal(port.sent.filter(p=>p.type==='track').length,64,'only 64 declarations reach even a legacy unscoped host');
  release();await pending;
  assert.equal((await packet(app,{type:'track',id:'replacement-64',kind:'remote',sampleRate:48000,channels:1},66)).error,undefined);
  await app.command(command('stop','stop'));
 }finally{release?.();app.close()}
});

for(const order of ['browser-first','hook-first'])test(`SPA generation changes exactly once (${order}), rejects late A evidence, and child changes cannot stop B`,async()=>{
 const app=await setup(3,{},['source-outcomes-v1']);
 const discover=(port,generation,count=1)=>port.onMessage.emit({type:'discovery',sourceCount:count,heard:count>0,documentGeneration:'document',callGeneration:generation});
 try{
  const page=app.pages[0];discover(page,'A');await app.command(command('start-A','start'));
  const before=app.api.controlStatus().sessions[0].callGeneration;
  app.tabs.get(2).url='https://meet.example/B';
  if(order==='browser-first'){
   app.chrome.tabs.onUpdated.emit(2,{url:'https://meet.example/B'});
   const once=app.api.callInfo(2).generation;
   discover(page,'A');assert.equal([...app.api.frames].find(f=>f.tabId===2&&f.frameId===0).heard,false,'in-flight A cannot vote under B');
   discover(page,'B');assert.equal((await app.api.meetings()).meetings[0].callGeneration,once);
  }else{discover(page,'B');const once=(await app.api.meetings()).meetings[0].callGeneration;await until(()=>app.audio[0].closed);app.chrome.tabs.onUpdated.emit(2,{url:'https://meet.example/B'});assert.equal((await app.api.meetings()).meetings[0].callGeneration,once)}
  const observed=(await app.api.meetings()).meetings[0];assert.equal(observed.heard,true);assert.equal(observed.sourceCount,1);
  await until(()=>app.audio[0].closed);
  discover(page,'B');const generation=(await app.api.meetings()).meetings[0].callGeneration;assert.notEqual(generation,before);
  await app.command(command('start-B','start',{callGeneration:generation}));
  discover(page,'B');assert.equal(app.audio[1].closed,false,'a late matching hook notification cannot stop B');
  assert.equal(app.api.controlStatus().sessions[0].callGeneration,generation);
  const child=app.frame(2,1,1);discover(child,'child-A');discover(child,'child-B');
  assert.equal(app.audio[1].closed,false);assert.equal(app.api.controlStatus().sessions[0].callGeneration,generation);
  await app.command(command('stop-B','stop'));
 }finally{app.close()}
});

test('a recovered transient retry does not send a false interrupted problem at Stop',async()=>{
 const app=await setup(3,{},['source-outcomes-v1']);
 try{
  await app.command(command('start','start'));
  app.pages[0].onMessage.emit({type:'warning',transient:true});
  await packet(app,{type:'track',id:'recovered',kind:'remote',sampleRate:48000,channels:1},1);
  await app.command(command('stop','stop'));
  assert.equal(app.audio[0].sent.find(p=>p.type==='stop').problem,null);
 }finally{app.close()}
});

test('whole-tab native-first Stop retires session, offscreen document and badge',async()=>{
 const app=await setup(3,{},['source-outcomes-v1']);let opened=false;const badges=[];
 const offscreen=packet=>new Promise(resolve=>app.chrome.runtime.onMessage.emit({target:'background',type:'packet',packet},{id:'extension',url:'chrome-extension://extension/offscreen.html'},resolve));
 try{
  app.chrome.tabCapture.getMediaStreamId=async()=>'granted';app.chrome.offscreen.createDocument=async()=>{opened=true};
  app.chrome.offscreen.closeDocument=async()=>{opened=false};app.chrome.runtime.getContexts=async()=>opened?[{}]:[];
  app.chrome.runtime.sendMessage=async()=>({});app.chrome.action.setBadgeText=async({text})=>badges.push(text);
  await app.api.start('tab',2);const port=app.audio[0],post=port.postMessage;
  assert.equal((await offscreen({type:'track',id:'tab',kind:'tab',sampleRate:48000,channels:1})).error,undefined);
  port.postMessage=function(p){if(p.type==='audio'){this.sent.push(p);queueMicrotask(()=>this.onMessage.emit({type:'error',code:'state',scope:'command',message:'Capture already saved'}))}else post.call(this,p)};
  assert.equal((await offscreen({type:'audio',id:'tab',sequence:0,offsetMs:0,pcm:'AAA='})).code,'state');
  await until(()=>port.closed&&!opened&&badges.at(-1)==='');assert.equal(app.api.controlStatus().recording,false);
  assert.equal(port.sent.some(p=>p.type==='sources'&&p.discoveredSources.some(s=>s.failed)),false);
  await app.api.start('tab',2);assert.equal(app.audio.length,2);await app.api.stop();
 }finally{app.close()}
});

test('retired and refused span packets never reach native or fail a healthy replacement root',async()=>{
 for(const features of [[],['source-outcomes-v1']]){
  const app=await setup(3,{},features);let release;
  try{
   await app.command(command('start','start'));const port=app.audio[0],post=port.postMessage;
   await packet(app,{type:'track',id:'old',sourceTrackId:'root',kind:'remote',sampleRate:48000,channels:1},0);
   if(features.length){
    port.postMessage=function(p){if(p.type==='trackEnd'){this.sent.push(p);release=()=>this.onMessage.emit({type:'ack'})}else post.call(this,p)};
    const retiring=packet(app,{type:'trackEnd',id:'old',reason:'ended',droppedFrames:0,endOffsetMs:null},1);await until(()=>release);
    for(const [i,type]of ['track','audio','trackEnd'].entries()){
     const ack=await packet(app,{type,id:'old',sourceTrackId:'root',kind:'remote',sampleRate:48000,channels:1,sequence:9,offsetMs:10,pcm:'AAA=',reason:'failed',droppedFrames:99},i+2);
     assert.equal(ack.code,'state');
    }
    assert.equal(port.sent.filter(p=>p.id==='old').length,2);release();await retiring;
    await packet(app,{type:'track',id:'new',sourceTrackId:'root',kind:'remote',sampleRate:48000,channels:1},6);
    assert.equal((await packet(app,{type:'audio',id:'new',sequence:0,offsetMs:10,pcm:'AAA='},7)).error,undefined);
   }
   assert.equal((await packet(app,{type:'audio',id:'never-admitted',sequence:0,offsetMs:0,pcm:'AAA='},8)).code,'state');
   assert.equal(port.sent.some(p=>p.id==='never-admitted'),false);assert.equal(port.closed,false);
   assert.deepEqual(Array.from(app.api.controlStatus().sessions[0].failedSources),[]);
   await app.command(command('stop','stop'));assert.equal(port.sent.find(p=>p.type==='stop').problem,null);
  }finally{release?.();app.close()}
 }
});

test('a refused in-flight source update cannot prevent the final native Stop',async()=>{
 const app=await setup(3,{},['source-outcomes-v1']);let release;
 try{
  await app.command(command('start','start'));const port=app.audio[0],post=port.postMessage;
  port.postMessage=function(p){if(p.type==='sources'){this.sent.push(p);release=()=>this.onMessage.emit({type:'error',scope:'command',code:'source',message:'Generated rejected update'})}else post.call(this,p)};
  const updating=packet(app,{type:'sources',discoveredSources:[{sourceTrackId:'root',kind:'remote'}]},1);await until(()=>release);
  const stopping=app.command(command('stop','stop'));await until(()=>app.pages[0].sent.some(p=>p.type==='stop'));release();await updating;await stopping;
  assert.equal(port.sent.some(p=>p.type==='stop'),true);assert.equal(port.closed,true);
 }finally{release?.();app.close()}
});

for(const order of ['browser-first','hook-first'])test(`idle SPA boundary and early B Start stay fenced once (${order})`,async()=>{
 const app=await setup(3,{},['source-outcomes-v1']);
 const discover=generation=>app.pages[0].onMessage.emit({type:'discovery',sourceCount:1,heard:true,documentGeneration:'doc',callGeneration:generation});
 try{
  discover('A');await app.api.meetings();app.tabs.get(2).url='https://meet.example/B';
  if(order==='browser-first')app.chrome.tabs.onUpdated.emit(2,{url:'https://meet.example/B'});else discover('B');
  const generation=app.api.callInfo(2).generation;
  if(order==='browser-first'){
   assert.equal((await app.command(command('before-inventory','start',{callGeneration:generation}))).ok,false);
   assert.equal(app.audio.length,0);discover('B');
  }
  assert.equal((await app.command(command('start-B','start',{callGeneration:generation}))).ok,true);
  if(order==='browser-first')discover('B');else app.chrome.tabs.onUpdated.emit(2,{url:'https://meet.example/B'});
  assert.equal(app.api.callInfo(2).generation,generation);assert.equal(app.audio[0].closed,false);
  const row=(await app.api.meetings()).meetings[0];assert.equal(row.heard,true);assert.equal(row.sourceCount,1);
  await app.command(command('stop-B','stop'));
 }finally{app.close()}
});

test('B to C rotates while A Stop is draining, without a second Stop or stale B evidence',async()=>{
 const app=await setup(3,{},['source-outcomes-v1']);let release;
 const page=app.pages[0],discover=generation=>page.onMessage.emit({type:'discovery',sourceCount:1,heard:true,documentGeneration:'doc',callGeneration:generation});
 try{
  discover('A');await app.command(command('start-A','start'));const post=page.postMessage;
  page.postMessage=function(p){if(p.type==='stop'){this.sent.push(p);release=()=>this.onMessage.emit({type:'stopped'})}else post.call(this,p)};
  app.tabs.get(2).url='https://meet.example/B';app.chrome.tabs.onUpdated.emit(2,{url:'https://meet.example/B'});discover('B');await until(()=>release);
  const B=app.api.callInfo(2).generation;
  app.tabs.get(2).url='https://meet.example/C';discover('C');const C=app.api.callInfo(2).generation;assert.notEqual(C,B);
  app.chrome.tabs.onUpdated.emit(2,{url:'https://meet.example/C'});discover('B');assert.equal(app.api.callInfo(2).generation,C);
  const row=(await app.api.meetings()).meetings[0];assert.equal(row.sourceCount,1);assert.equal(row.heard,true);
  assert.equal(page.sent.filter(p=>p.type==='stop').length,1);release();await until(()=>app.audio[0].closed);
 }finally{release?.();app.close()}
});


test('whole-tab warnings preserve a source-specific readiness error until that source recovers',async()=>{
 const app=await setup(3,{},['source-outcomes-v1']);
 const report=message=>new Promise(resolve=>app.chrome.runtime.onMessage.emit({target:'background',...message},{id:'extension',url:'chrome-extension://extension/offscreen.html'},resolve));
 try{
  app.chrome.tabCapture.getMediaStreamId=async()=>'granted';app.chrome.offscreen.createDocument=async()=>{};
  app.chrome.runtime.sendMessage=async()=>({});
  await app.api.start('tab',2);
  const source=app.audio[0].sent.find(p=>p.type==='sources').discoveredSources[0].sourceTrackId;
  await report({type:'warning',sourceTrackId:'stale-tab-source',message:'Old source error',transient:true});
  assert.deepEqual(Array.from(app.api.controlStatus().failedSources),[]);
  await report({type:'warning',sourceTrackId:source,message:'Audio render clock did not become ready.',transient:true});
  assert.deepEqual(Array.from(app.api.controlStatus().failedSources),[source]);
  await report({type:'packet',packet:{type:'track',id:'tab',sourceTrackId:source,kind:'tab',sampleRate:48000,channels:1}});
  assert.equal(app.api.controlStatus().message,'Audio render clock did not become ready.');
  assert.deepEqual(Array.from(app.api.controlStatus().failedSources),[source]);
  await report({type:'warning',sourceTrackId:source,recovered:true,transient:true});
  assert.deepEqual(Array.from(app.api.controlStatus().failedSources),[]);
  await app.api.stop();
 }finally{app.close()}
});

test('I/O refusal fences retry cleanup and in-flight chunks without stopping healthy PCM or microphone',async()=>{
 const app=await setup(3,{},['source-outcomes-v1']);
 try{
  await app.command(command('start','start',{microphone:true}));
  const port=app.audio[0];
  assert.equal(port.sent[0].microphone,true);
  for(const id of ['A','B'])await packet(app,{type:'track',id,sourceTrackId:'root-'+id,kind:'remote',sampleRate:48000,channels:1},'track-'+id);
  let paused=true,draining=false;
  const queue=[];
  const drain=async()=>{
   if(draining||paused)return;draining=true;
   try{while(queue.length&&!paused){const value=queue.shift();
    port.onMessage.emit(value.id==='B'&&value.type==='audio'
     ?{type:'error',code:value.sequence===0?'io':'state',scope:'source',id:'B',message:'Generated B write/retired failure'}
     :{type:'ack'});
    await Promise.resolve();
   }}finally{draining=false;}
  };
  port.postMessage=function(value){this.sent.push(value);queue.push(value);void drain();};
  const inFlight=[0,1,2,3].map(sequence=>packet(app,{type:'audio',id:'B',sequence,offsetMs:sequence*10,pcm:'AAA='},'B-'+sequence));
  await until(()=>queue.length===4);paused=false;await drain();
  const replies=await Promise.all(inFlight);
  assert.equal(replies[0].code,'io');assert.ok(replies.slice(1).every(p=>p.retired===true));
  const before=port.sent.length;
  const cleanup=await packet(app,{type:'trackEnd',id:'B',reason:'retry',droppedFrames:0,endOffsetMs:null},'cleanup');
  assert.equal(cleanup.retired,true);assert.equal(port.sent.length,before,'retired cleanup is local');
  assert.equal((await packet(app,{type:'audio',id:'A',sequence:0,offsetMs:0,pcm:'AAA='},'A-healthy')).error,undefined);
  await packet(app,{type:'track',id:'B-retry',sourceTrackId:'root-B',kind:'remote',sampleRate:48000,channels:1},'B-track-retry');
  assert.equal((await packet(app,{type:'audio',id:'B-retry',sequence:0,offsetMs:1000,pcm:'AAA='},'B-recovered')).error,undefined);
  assert.equal(app.api.controlStatus().sessions.length,1);assert.equal(port.closed,false);
  assert.equal(port.sent.some(p=>p.type==='stop'),false);
  await app.command(command('stop','stop'));
 }finally{app.close();}
});

for(const mode of ['tracks','tab'])test(`native retired-span marker crosses ${mode} transport without retiring the capture`,async()=>{
 const app=await setup(3,{},['source-outcomes-v1']);let opened=false;
 const offscreen=value=>new Promise(resolve=>app.chrome.runtime.onMessage.emit({target:'background',type:'packet',packet:value},{id:'extension',url:'chrome-extension://extension/offscreen.html'},resolve));
 const send=(p,id)=>mode==='tab'?offscreen(p):packet(app,p,id);
 try{
  if(mode==='tab'){
   app.chrome.tabCapture.getMediaStreamId=async()=>'granted';app.chrome.offscreen.createDocument=async()=>{opened=true};
   app.chrome.offscreen.closeDocument=async()=>{opened=false};app.chrome.runtime.getContexts=async()=>opened?[{}]:[];
   app.chrome.runtime.sendMessage=async()=>({});await app.api.start('tab',2);
  }else await app.command(command('start','start'));
  const port=app.audio[0],post=port.postMessage;
  await send({type:'track',id:'B',kind:mode==='tab'?'tab':'remote',sampleRate:48000,channels:1},'track-B');
  port.postMessage=function(p){if(p.type==='audio'&&p.id==='B'){this.sent.push(p);queueMicrotask(()=>this.onMessage.emit({type:'error',code:'state',scope:'source',retired:true,id:'B',message:'Only this WAV was retired'}));}else post.call(this,p);};
  const refused=await send({type:'audio',id:'B',sequence:0,offsetMs:0,pcm:'AAA='},'retired-B');
  assert.equal(refused.retired,true);assert.equal(port.closed,false);assert.equal(app.api.controlStatus().sessions.length,1);
  assert.equal(port.sent.some(p=>p.type==='stop'),false);
  if(mode==='tab')assert.equal(opened,true);
  await app.api.stop();
 }finally{app.close();}
});
