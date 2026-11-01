const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const source=fs.readFileSync(path.join(__dirname,'../capture.js'),'utf8');
async function mocked(run) {
 const saved=new Map(['AudioContext','AudioWorkletNode','Audio','performance'].map(key=>[key,Object.getOwnPropertyDescriptor(globalThis,key)]));
 const nodes=[],contexts=[],renderers=[];let now=1000;
 class Graph {connect(value){return value}disconnect(){this.disconnected=true}}
 class Context {
  sampleRate=48000;state='running';currentTime=0;destination=new Graph();listeners=new Set();audioWorklet={addModule:async()=>{}};
  constructor(){contexts.push(this)}createMediaStreamSource(){return new Graph()}createGain(){return Object.assign(new Graph(),{gain:{value:0}})}
  addEventListener(_event,fn){this.listeners.add(fn)}removeEventListener(_event,fn){this.listeners.delete(fn)}
  getOutputTimestamp(){return {contextTime:1,performanceTime:1000}}
  async resume(){if(this.state==='suspended'){now+=500;this.state='running';this.listeners.forEach(fn=>fn())}}
  async close(){this.state='closed'}
 }
 class Worklet extends Graph {connect(target){if(!(typeof Context.deferReadiness==="function"?Context.deferReadiness(this):Context.deferReadiness))queueMicrotask(()=>this.port.onmessage?.({data:{ready:true}}));return target}constructor(){super();nodes.push(this);this.port={postMessage:message=>{if(message==="stop")this.port.onmessage({data:{stopped:true}})}}}}
 globalThis.AudioContext=Context;globalThis.AudioWorkletNode=Worklet;
 globalThis.Audio=class {constructor(){renderers.push(this)}async play(){}pause(){this.paused=true}};
 Object.defineProperty(globalThis,'performance',{configurable:true,value:{timeOrigin:100000,now:()=>now}});
 try {const module=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));await run({...module,nodes,contexts,renderers,Context,setNow:value=>{now=value}})}
 finally {for(const [key,descriptor] of saved){if(descriptor)Object.defineProperty(globalThis,key,descriptor);else delete globalThis[key]}}
}
const tick=()=>new Promise(resolve=>setImmediate(resolve));
test('transient suspension resumes without source failure and preserves the elapsed gap',async()=>mocked(async({capture,nodes,contexts,setNow})=>{
 const packets=[],failures=[];const recorder=await capture({stream:{},id:'original',label:'Audio',kind:'local',origin:100000,workletURL:'',send:async p=>packets.push(p),fail:m=>failures.push(m)});
 nodes[0].port.onmessage({data:{pcm:new ArrayBuffer(8192*2),frame:0}});await tick();
 const context=contexts[0];context.currentTime=8192/48000;setNow(1500);context.state='suspended';context.listeners.forEach(fn=>fn());await tick();await tick();
 nodes[0].port.onmessage({data:{pcm:new ArrayBuffer(8192*2),frame:8192}});await recorder.stop();
 const chunks=packets.filter(p=>p.type==='audio');assert.equal(chunks.length,2);assert.equal(packets.filter(p=>p.type==='track').length,1);
 assert.ok(Math.abs(chunks[1].offsetMs-chunks[0].offsetMs-8192/48-500)<0.001);assert.deepEqual(failures,[]);
}));
function clock() {
 let now=1000, serial=0; const timers=new Map();
 return {now:()=>now, timers,
  schedule(fn,ms){const id=++serial;timers.set(id,{fn,at:now+ms});return id},
  cancel(id){timers.delete(id)},
  async advance(ms){now+=ms;for(const [id,timer]of [...timers])if(timer.at<=now){timers.delete(id);await timer.fn()}},
 };
}
test('live source retries beyond three attempts, preserving identity and closing each old span first',async()=>mocked(async({capture,nodes,Context,retryDelay})=>{
 const packets=[],failures=[],time=clock();const context=new Context();
 const recorder=await capture({stream:{},id:'original',sourceTrackId:'receiver-root',reportOutcomes:true,label:'Guest',kind:'local',origin:100000,workletURL:'',sharedContext:context,
  clock:time.now,schedule:time.schedule,cancel:time.cancel,send:async p=>packets.push(p),fail:m=>failures.push(m)});
 await time.advance(179000); // Worklet failure at minute three, without a wall-clock wait.
 for(let index=0;index<5;index++){
  nodes[index].onprocessorerror();
  assert.equal(recorder.state,'retrying');
  await time.advance(retryDelay(index)-1);assert.equal(nodes.length,index+1);
  await time.advance(1);assert.equal(nodes.length,index+2);
  assert.equal(recorder.failed,false);
 }
 const tracks=packets.filter(p=>p.type==='track');assert.equal(tracks.length,6);
 assert.equal(new Set(tracks.map(p=>p.id)).size,6);
 assert.ok(tracks.every(p=>p.sourceTrackId==='receiver-root'&&p.label==='Guest'));
 for(let i=1;i<tracks.length;i++){
  const opened=packets.indexOf(tracks[i]);const ended=packets.findIndex(p=>p.type==='trackEnd'&&p.id===tracks[i-1].id);
  assert.ok(ended>=0&&ended<opened,'the old span ends before its replacement opens');
  assert.equal(packets[ended].reason,'retry','recovery rotations must not mark an old span failed');
 }
 assert.deepEqual(failures,[]);await recorder.stop();assert.equal(time.timers.size,0);
}));
test('first attach failures are retried and Stop cancels future work',async()=>mocked(async({capture,nodes,Context})=>{
 const time=clock(),packets=[],context=new Context();let attempts=0;
 context.audioWorklet.addModule=async()=>{if(++attempts<3)throw Error('generated module failure')};
 const recorder=await capture({stream:{},id:'first',sourceTrackId:'first-source',label:'Guest',kind:'local',origin:100000,workletURL:'',sharedContext:context,
  clock:time.now,schedule:time.schedule,cancel:time.cancel,send:async p=>packets.push(p)});
 assert.equal(recorder.state,'retrying');assert.equal(nodes.length,0);
 await time.advance(1000);assert.equal(attempts,2);
 await time.advance(5000);assert.equal(nodes.length,1);
 nodes[0].port.onmessage({data:{pcm:new ArrayBuffer(8192*2),frame:0}});await tick();assert.equal(recorder.state,'recording');
 nodes[0].onprocessorerror();assert.equal(time.timers.size,1);
 await recorder.stop();await time.advance(90000);assert.equal(nodes.length,1);assert.equal(time.timers.size,0);
}));
test('sixty seconds of acknowledged zero PCM reset source retry backoff',async()=>mocked(async({capture,nodes,Context,retryDelay})=>{
 const time=clock(),context=new Context();
 const recorder=await capture({stream:{},id:'healthy',label:'Audio',kind:'local',origin:100000,workletURL:'',sharedContext:context,
  clock:time.now,schedule:time.schedule,cancel:time.cancel,send:async()=>{}});
 for(let index=0;index<3;index++){nodes[index].onprocessorerror();await time.advance(retryDelay(index))}
 nodes[3].port.onmessage({data:{pcm:new ArrayBuffer(8192*2),frame:0}});await tick();
 await time.advance(60000);
 nodes[3].port.onmessage({data:{pcm:new ArrayBuffer(8192*2),frame:8192}});await tick();
 nodes[3].onprocessorerror();assert.equal([...time.timers.values()][0].at-time.now(),1000);
 await recorder.stop();
}));
test('source retry policy is pure and uses monotonic deadlines',async()=>mocked(async({reconcile})=>{
 for(const [state,attempts,last,now,expected]of [
  [{live:true,status:'recording'},0,0,90000,'none'],
  [{live:true,failedAt:null},0,null,0,'reattach'],
  [{live:true,failedAt:0},12,null,29999,'none'],
  [{live:true,failedAt:0},12,null,30000,'reattach'],
  [{live:true,failedAt:60000},12,0,61000,'reattach'],
  [{live:false,failedAt:0},0,null,90000,'giveUpAndReport'],
  [{live:true,stopping:true},0,null,0,'giveUpAndReport'],
 ]) {const original=JSON.stringify(state);assert.equal(reconcile(state,attempts,last,now),expected);assert.equal(JSON.stringify(state),original)}
}));

test('a ninety-second suspension rotates the source before sending an oversized gap',async()=>mocked(async({capture,nodes,Context})=>{
 const time=clock(),packets=[],context=new Context();
 const recorder=await capture({stream:{},id:'before',sourceTrackId:'stable',label:'Audio',kind:'local',origin:100000,workletURL:'',sharedContext:context,
  clock:time.now,schedule:time.schedule,cancel:time.cancel,send:async p=>packets.push(p)});
 nodes[0].port.onmessage({data:{pcm:new ArrayBuffer(8192*2),frame:0}});await tick();
 nodes[0].port.onmessage({data:{pcm:new ArrayBuffer(8192*2),frame:48000*90}});await tick();
 assert.equal(recorder.state,'retrying');assert.equal(packets.filter(p=>p.type==='audio').length,1);
 await time.advance(1000);assert.equal(nodes.length,2);
 nodes[1].port.onmessage({data:{pcm:new ArrayBuffer(8192*2),frame:48000*90+8192}});await tick();
 const chunks=packets.filter(p=>p.type==='audio');assert.equal(chunks.length,2);assert.notEqual(chunks[0].id,chunks[1].id);
 assert.equal(new Set(packets.filter(p=>p.type==='track').map(p=>p.sourceTrackId)).size,1);
 assert.equal(recorder.droppedFrames,8192);await recorder.stop();
}));

test('intentional call fencing ends the span without retries or a false source failure',async()=>mocked(async({capture,nodes,Context})=>{
 const time=clock(),packets=[],warnings=[],failures=[],context=new Context();let chunks=0;
 const recorder=await capture({stream:{},id:'call-A',sourceTrackId:'receiver-A',reportOutcomes:true,label:'Guest',kind:'local',origin:100000,workletURL:'',sharedContext:context,
  clock:time.now,schedule:time.schedule,cancel:time.cancel,warn:m=>warnings.push(m),fail:m=>failures.push(m),
  send:async p=>{if(p.type==='audio'&&++chunks===2)throw Object.assign(new Error('Call changed'),{code:'callChanged'});packets.push(p)}});
 nodes[0].port.onmessage({data:{pcm:new ArrayBuffer(8192*2),frame:0}});await tick();
 nodes[0].port.onmessage({data:{pcm:new ArrayBuffer(8192*2),frame:8192}});await tick();
 assert.equal(time.timers.size,0,'old call must never retry in the new call');
 assert.deepEqual(warnings,[]);assert.deepEqual(failures,[]);
 await recorder.stop('callChanged');
 const ended=packets.find(p=>p.type==='trackEnd');assert.equal(ended.reason,'callChanged');
 assert.equal(packets.filter(p=>p.type==='audio').length,1);assert.ok(ended.endOffsetMs>0);
}));

test('native-first Stop retires pending PCM and TrackEnd without retries or false IO warnings',async()=>mocked(async({capture,nodes,Context})=>{
 const time=clock(),warnings=[],failures=[],context=new Context();let chunks=0;
 const ended=()=>Object.assign(new Error('Capture already saved'),{code:'state',scope:'source'});
 const recorder=await capture({stream:{},id:'native-stopped',sourceTrackId:'receiver',reportOutcomes:true,label:'Guest',kind:'local',origin:100000,workletURL:'',sharedContext:context,
  clock:time.now,schedule:time.schedule,cancel:time.cancel,warn:m=>warnings.push(m),fail:m=>failures.push(m),
  send:async p=>{if(p.type==='trackEnd'||p.type==='audio'&&++chunks===2)throw ended()}});
 nodes[0].port.onmessage({data:{pcm:new ArrayBuffer(8192*2),frame:0}});await tick();
 nodes[0].port.onmessage({data:{pcm:new ArrayBuffer(8192*2),frame:8192}});await tick();
 assert.equal(time.timers.size,0);await recorder.stop();
 assert.deepEqual(warnings,[]);assert.deepEqual(failures,[]);
}));

test('production page quota survives seventy real worklet failures and then records audio',async()=>mocked(async({capture,nodes,Context,retryDelay})=>{
 const fixture=require('./support/page-capture-harness.cjs'),time=clock();let attempts=0;
 class FlakyContext extends Context {constructor(){super();this.audioWorklet.addModule=async()=>{if(++attempts<=70)throw Error('generated initial worklet failure')}}}
 await fixture(async f=>{
  f.peer.senders=[{track:{id:'mic',label:'Microphone',kind:'audio',readyState:'live',enabled:true,muted:false},getParameters:()=>({encodings:[]})}];
  await f.start('seventy-initial-failures',{sourceOutcomes:true});
  for(let i=0;i<70;i++)await time.advance(retryDelay(i));
  assert.equal(attempts,71);assert.equal(nodes.length,1);
  nodes[0].port.onmessage({data:{pcm:new ArrayBuffer(8192*2),frame:0}});await tick();
  assert.equal(f.captures[0].recorder.state,'recording');
  assert.equal(f.messages.filter(m=>m.type==='packet'&&m.packet.type==='track').length,1);
  assert.ok(f.messages.filter(m=>m.type==='packet'&&m.packet.type==='sources').every(m=>m.packet.discoveredSources.every(s=>!s.failed)));
  await f.stop();
 },{Context:FlakyContext,capture:options=>capture({...options,clock:time.now,schedule:time.schedule,cancel:time.cancel})});
}));

test('a real allocation limit is terminal for that track with no thirty-second retry loop',async()=>mocked(async({capture,nodes,Context})=>{
 const time=clock(),failures=[];
 const recorder=await capture({stream:{},id:'allocated',sourceTrackId:'root',kind:'local',label:'Guest',origin:100000,workletURL:'',sharedContext:new Context(),
  nextSpanId:()=>null,clock:time.now,schedule:time.schedule,cancel:time.cancel,send:async()=>{},fail:m=>failures.push(m)});
 nodes[0].onprocessorerror();await time.advance(1000);
 assert.equal(recorder.failed,true);assert.equal(time.timers.size,0);await time.advance(600000);
 assert.equal(nodes.length,1);assert.equal(failures.length,1);await assert.rejects(recorder.stop(),/track limit/);
}));

test('a refused live track is terminal and rescans never reattach or re-warn it',async()=>mocked(async({capture,Context})=>{
 const fixture=require('./support/page-capture-harness.cjs'),time=clock();
 await fixture(async f=>{
  f.peer.senders=[{track:{id:'mic',label:'Microphone',kind:'audio',readyState:'live',enabled:true,muted:false},getParameters:()=>({encodings:[]})}];
  await f.start('refused',{sourceOutcomes:true});assert.equal(f.captures[0].recorder.failed,true);
  const warnings=f.messages.filter(m=>m.type==='warning').length;assert.equal(warnings,1);
  for(let i=0;i<5;i++)await f.scan();
  assert.equal(f.captures.length,1);assert.equal(f.messages.filter(m=>m.type==='warning').length,warnings);assert.equal(time.timers.size,0);
  await f.stop();assert.equal(f.messages.filter(m=>m.type==='warning').length,1);
 },{Context,capture:options=>capture({...options,clock:time.now,schedule:time.schedule,cancel:time.cancel,send:packet=>packet.type==='track'?Promise.reject(Object.assign(new Error('Generated native quota'),{code:'limit',scope:'source'})):options.send(packet)})});
}));

test('Stop retains a source still retrying before its first track was opened',async()=>mocked(async({capture,Context})=>{
 const fixture=require('./support/page-capture-harness.cjs'),time=clock();
 class Unavailable extends Context {constructor(){super();this.audioWorklet.addModule=async()=>{throw Error('generated initial attach failure')}}}
 await fixture(async f=>{
  f.peer.senders=[{track:{id:'mic',label:'Microphone',kind:'audio',readyState:'live',enabled:true,muted:false},getParameters:()=>({encodings:[]})}];
  await f.start('unrecovered',{sourceOutcomes:true});assert.equal(f.captures[0].recorder.state,'retrying');await f.stop();
  const records=f.messages.filter(m=>m.type==='packet'&&m.packet.type==='sources').flatMap(m=>m.packet.discoveredSources);
  assert.equal(records.at(-1).failed,true);assert.equal(time.timers.size,0);
 },{Context:Unavailable,capture:options=>capture({...options,clock:time.now,schedule:time.schedule,cancel:time.cancel})});
}));

test('stopped span ignores retained worklet callbacks and cannot pump audio after TrackEnd',async()=>mocked(async({capture,nodes,Context})=>{
 const packets=[],context=new Context();
 const recorder=await capture({stream:{},id:'closed',sourceTrackId:'root',kind:'local',label:'Mic',origin:100000,workletURL:'',sharedContext:context,reportOutcomes:true,send:async p=>packets.push(p)});
 const late=nodes[0].port.onmessage;context.state='closed';await recorder.stop();
 late({data:{pcm:new ArrayBuffer(256),frame:0}});await tick();
 assert.equal(nodes[0].port.onmessage,null);assert.equal(packets.filter(p=>p.type==='audio').length,0);
 assert.equal(packets.at(-1).type,'trackEnd');
}));

test('old in-flight ACK after processor failure cannot mark a retry healthy',async()=>mocked(async({capture,nodes,Context})=>{
 const time=clock();let acknowledge,healthy=0;
 const recorder=await capture({stream:{},id:'old',kind:'local',label:'Mic',origin:100000,workletURL:'',sharedContext:new Context(),clock:time.now,schedule:time.schedule,cancel:time.cancel,onAudio:()=>healthy++,send:p=>p.type==='audio'?new Promise(resolve=>acknowledge=resolve):Promise.resolve()});
 nodes[0].port.onmessage({data:{pcm:new ArrayBuffer(256),frame:0}});await tick();assert.ok(acknowledge);
 nodes[0].onprocessorerror();assert.equal(recorder.state,'retrying');acknowledge();await tick();
 assert.equal(recorder.state,'retrying');assert.equal(healthy,0);await recorder.stop();
}));

test('Stop drains admitted PCM and worklet final flush before ending the span',async()=>mocked(async({capture,nodes,Context})=>{
 const packets=[];let release;
 const recorder=await capture({stream:{},id:'flush',kind:'local',label:'Mic',origin:100000,workletURL:'',sharedContext:new Context(),reportOutcomes:true,send:p=>{packets.push(p);return p.type==='audio'&&p.sequence===0?new Promise(resolve=>release=resolve):Promise.resolve()}});
 for(let i=0;i<6;i++)nodes[0].port.onmessage({data:{pcm:new ArrayBuffer(256),frame:i*128}});
 await tick();assert.ok(release);
 nodes[0].port.postMessage=()=>{nodes[0].port.onmessage({data:{pcm:new ArrayBuffer(256),frame:768}});nodes[0].port.onmessage({data:{stopped:true}})};
 const stopping=recorder.stop();release();await stopping;
 assert.equal(packets.filter(p=>p.type==='audio').length,7);assert.equal(packets.at(-1).type,'trackEnd');assert.equal(nodes[0].port.onmessage,null);
}));

test('source connects only after render readiness and PCM arms only after the native declaration ACK',async()=>mocked(async({capture,nodes,Context})=>{
 Context.deferReadiness=true;
 const context=new Context(),packets=[],commands=[];let connected=false,ack;
 context.createMediaStreamSource=()=>({connect(){connected=true},disconnect(){}});
 const declaration=new Promise(resolve=>{ack=resolve});
 const pending=capture({stream:{},id:'clock-ready',kind:'local',label:'Voice',origin:100000,workletURL:'',sharedContext:context,
  send:async packet=>{packets.push(packet);if(packet.type==='track')await declaration}});
 await tick();
 assert.equal(nodes.length,1);assert.equal(connected,false);assert.deepEqual(packets,[]);
 const node=nodes[0];
 node.port.postMessage=message=>{commands.push(message);if(message==='stop')node.port.onmessage({data:{stopped:true}})};
 node.port.onmessage({data:{ready:true}});await tick();
 assert.equal(connected,true);assert.equal(packets[0].type,'track');assert.deepEqual(commands,[]);
 ack();const recorder=await pending;
 assert.deepEqual(commands,['start']);await recorder.stop();
}));


for(const recover of [false,true])test(`a timed-out source stays visible through relay/status/retries (${recover?'recovered':'still failing'}) while healthy audio continues`,async()=>mocked(async({capture,nodes,contexts,renderers,Context})=>{
 const vm=require('node:vm'),{setup,until,event}=require('./support/app-control-harness.cjs');
 const pageFixture=require('./support/page-capture-harness.cjs'),time=clock();
 const app=await setup(2,{},['source-outcomes-v1']);
 const frame=app.pages[0],endpoint={onMessage:event(),onDisconnect:event(),postMessage:packet=>frame.onMessage.emit(packet)};
 frame.postMessage=packet=>{frame.sent.push(packet);endpoint.onMessage.emit(packet)};
 const listeners=new Map(),queued=[];let receive,pageStart;
 const window={addEventListener:(name,fn)=>listeners.set(name,fn),postMessage:packet=>{
  if(!receive){queued.push(packet);return;}
  const task=receive(packet);if(packet.type==='start')pageStart=task;
 }};
 const deliver=packet=>listeners.get('message')({source:window,data:packet});
 vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../relay.js'),'utf8'),{window,chrome:{runtime:{connect:()=>endpoint,onMessage:event(),getURL:name=>name==='capture.js'?pageFixture.moduleURL:name}}});
 Context.deferReadiness=node=>nodes.indexOf(node)!==1;
 try{
  await pageFixture(async f=>{
   receive=f.receive;
   const bad=f.remote('Waiting voice'),good=f.remote('Healthy voice');
   await f.tick();bad.packets=good.packets=10;await f.tick();bad.packets=good.packets=20;await f.tick();
   for(const packet of queued.splice(0))await receive(packet);
   const started=await app.command({type:'command',requestId:'start-clock',action:'start',tabId:2,mode:'tracks',independent:true,captureLocal:true});
   assert.equal(started.ok,true);
   try { await until(()=>nodes.length===1); } catch(error) { throw new Error(JSON.stringify({captures:f.captures.length,page:f.messages,control:frame.sent}),{cause:error}); }
   await tick();
   await time.advance(4000);await pageStart;
   assert.equal(f.captures[0].recorder.state,'retrying');assert.equal(nodes.length,2);
   assert.equal(nodes[0].port.onmessage,null);assert.equal(nodes[0].disconnected,true);
   assert.equal(renderers[0].paused,true);assert.equal(renderers[0].srcObject,null);
   assert.equal(contexts[0].state,'running','the shared healthy context stays open');
   const source=f.captures[0].sourceTrackId,other=f.captures[1].sourceTrackId;
   const status=()=>app.api.controlStatus();
   assert.deepEqual(Array.from(status().failedSources),[source]);
   assert.equal(status().message,'Audio render clock did not become ready.');
   const healthyID=app.audio[0].sent.find(p=>p.type==='track'&&p.sourceTrackId===other).id;
   nodes[1].port.onmessage({data:{pcm:new ArrayBuffer(256),frame:128}});
   await until(()=>app.audio[0].sent.some(p=>p.type==='audio'&&p.id===healthyID));
   assert.deepEqual(Array.from(status().failedSources),[source]);
   assert.equal(status().message,'Audio render clock did not become ready.','another declaration/PCM must not clear this source');
   const retry=time.advance(1000);await tick();assert.equal(nodes.length,3);
   await time.advance(4000);await retry;
   assert.equal(nodes[2].disconnected,true);assert.deepEqual(Array.from(status().failedSources),[source]);
   assert.equal(f.messages.filter(p=>p.type==='warning'&&p.sourceTrackId===source&&!p.recovered).length,1,'unchanged retries do not re-warn');
   nodes[1].port.onmessage({data:{pcm:new ArrayBuffer(256),frame:256}});
   await until(()=>app.audio[0].sent.filter(p=>p.type==='audio'&&p.id===healthyID).length===2);
   assert.equal(app.audio[0].closed,false);assert.equal(status().recording,true);
   if(recover){
    Context.deferReadiness=false;await time.advance(5000);
    assert.equal(nodes.length,4);assert.deepEqual(Array.from(status().failedSources),[source],'a new declaration alone is not recovery');
    nodes[3].port.onmessage({data:{pcm:new ArrayBuffer(256),frame:48000}});
    await until(()=>status().failedSources.length===0);
    assert.notEqual(status().message,'Audio render clock did not become ready.');
   }
   assert.equal((await app.command({type:'command',requestId:'stop-clock',action:'stop',tabId:2,independent:true})).ok,true);
   assert.equal(app.audio[0].sent.find(p=>p.type==='stop').problem,recover?null:'Audio render clock did not become ready.');
   const failed=app.audio[0].sent.filter(p=>p.type==='sources').flatMap(p=>p.discoveredSources).filter(s=>s.failed);
   assert.deepEqual(failed.map(s=>s.sourceTrackId),recover?[]:[source]);
  },{Context,capture:options=>capture({...options,clock:time.now,schedule:time.schedule,cancel:time.cancel}),deliver});
 }finally{app.close()}
}));

for(const first of ['io','retired'])test(`source-local ${first} refusal retries a new span after retired cleanup while healthy PCM continues`,async()=>mocked(async({capture,nodes,Context,retryDelay})=>{
 const time=clock(),packets=[],pending=[],warnings=[],failures=[],context=new Context();
 const ended=()=>Object.assign(new Error('Only this source span ended'),{code:'state',scope:'source',retired:true});
 const send=async p=>{
  packets.push(p);
  if(p.id==='B'&&p.type==='audio')return new Promise((resolve,reject)=>pending.push({resolve,reject}));
  if(p.id==='B'&&p.type==='trackEnd')throw ended();
 };
 const options={stream:{},reportOutcomes:true,label:'Guest',kind:'local',origin:100000,workletURL:'',sharedContext:context,
  clock:time.now,schedule:time.schedule,cancel:time.cancel,send,fail:m=>failures.push(m)};
 const a=await capture({...options,id:'A',sourceTrackId:'root-A'});
 const b=await capture({...options,id:'B',sourceTrackId:'root-B',nextSpanId:()=> 'B-retry',warn:(m,d)=>warnings.push(d.kind)});
 const emit=(node,frame)=>node.port.onmessage({data:{pcm:new ArrayBuffer(256),frame}});
 for(let i=0;i<4;i++)emit(nodes[1],i*128);
 const deadline=Date.now()+2000;
 while(pending.length<4){assert.ok(Date.now()<deadline,'Four admitted source chunks were not in flight');await tick();}
 pending[0].reject(first==='io'?Object.assign(new Error('B disk full'),{code:'io',scope:'source'}):ended());
 pending[1].resolve();pending[2].reject(ended());pending[3].reject(ended());
 while(b.state!=='retrying'){assert.ok(Date.now()<deadline,'B did not enter retry state');await tick();}
 emit(nodes[0],0);await tick();
 assert.equal(a.state,'recording');assert.equal(time.timers.size,1);
 await time.advance(retryDelay(0));
 assert.equal(nodes.length,3);emit(nodes[2],512);emit(nodes[0],128);await tick();
 assert.equal(b.state,'recording');assert.equal(a.state,'recording');
 assert.ok(packets.findIndex(p=>p.type==='trackEnd'&&p.id==='B') < packets.findIndex(p=>p.type==='track'&&p.id==='B-retry'));
 assert.equal(packets.filter(p=>p.type==='audio'&&p.id==='A').length,2);
 assert.equal(packets.filter(p=>p.type==='audio'&&p.id==='B-retry').length,1);
 assert.deepEqual(failures,[]);assert.deepEqual(warnings,['retry','recovered']);
 await a.stop();await b.stop();
}));
