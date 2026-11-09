// Native media is modeled here; the CI study runs the packaged hook on real RTC.
const {test}=require('node:test');const assert=require('node:assert/strict');
const vm=require('node:vm'),fs=require('node:fs'),path=require('node:path');
const {until}=require('./support/app-control-harness.cjs');
function fixture(){
  let now=0;const origin=1000000,timers=[],listeners=[],messages=[];
  const mapEach=Map.prototype.forEach;
  class Report extends Map {forEach(fn){return Reflect.apply(mapEach,this,[fn])}}
  class Track {
    #kind='audio';#state='live';#enabled=true;#muted=false;
    get kind(){return this.#kind}get readyState(){return this.#state}set readyState(value){this.#state=value}
    get enabled(){return this.#enabled}set enabled(value){this.#enabled=value}
    get muted(){return this.#muted}set muted(value){this.#muted=value}
    clone(){return new Track()}
  }
  class Stream {constructor(track){this.tracks=[track]}getAudioTracks(){return this.tracks}}
  class Sender {
    #track;constructor(track){this.#track=track;this.packets=0;this.age=0;this.pending=null}
    get track(){return this.#track}set track(value){this.#track=value}
    async getStats(){if(this.pending)await this.pending;return new Report([['out',{id:'out',type:'outbound-rtp',kind:'audio',packetsSent:this.packets,timestamp:origin+now-this.age}]])}
  }
  class Receiver {
    #track=new Track();constructor(){this.packets=0;this.age=0}
    get track(){return this.#track}
    async getStats(){return new Report([['in',{id:'in',type:'inbound-rtp',kind:'audio',packetsReceived:this.packets,timestamp:origin+now-this.age}]])}
  }
  class Transceiver {
    #sender;#receiver;#direction='sendrecv';constructor(sender,receiver){this.#sender=sender;this.#receiver=receiver}
    get sender(){return this.#sender}get receiver(){return this.#receiver}
    get currentDirection(){return this.#direction}set currentDirection(value){this.#direction=value}
  }
  class Peer {
    #connection='connected';#signal='stable';constructor(){this.slots=[]}
    get connectionState(){return this.#connection}get iceConnectionState(){return this.#connection}get signalingState(){return this.#signal}
    getTransceivers(){return this.slots}setRemoteDescription(){return Promise.resolve()}
    close(){this.#signal='closed';this.#connection='closed'}
  }
  const location={get href(){return 'https://neutral.invalid/call'}};
  const window={RTCPeerConnection:Peer,location,postMessage:packet=>messages.push(packet),addEventListener:(name,fn)=>listeners.push(fn)};
  const context=vm.createContext({isSecureContext:true,window,location,RTCPeerConnection:Peer,RTCRtpSender:Sender,RTCRtpReceiver:Receiver,RTCRtpTransceiver:Transceiver,
    MediaStreamTrack:Track,MediaStream:Stream,RTCStatsReport:Report,
    navigator:{mediaDevices:{async getUserMedia(){return new Stream(new Track())}}},
    crypto:{randomUUID:()=> 'hook-document'},performance:{timeOrigin:origin,now:()=>now},
    setInterval:fn=>timers.push(fn),setTimeout,clearTimeout});
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../rtc-evidence.js'),'utf8'),context);
  async function peer(kind='captured'){
    let track=kind==='synthetic'?new Track():(await context.navigator.mediaDevices.getUserMedia()).getAudioTracks()[0];
    if(kind==='clone')track=track.clone();
    const pc=new context.RTCPeerConnection(),sender=new Sender(track),receiver=new Receiver();
    pc.slots.push(new Transceiver(sender,receiver));
    return {pc,sender,receiver,track};
  }
  const read=()=>context.__cassiniCallEvidenceV1.read();
  const advance=ms=>{now+=ms};
  async function tick(peers){advance(1000);for(const {sender,receiver} of peers){sender.packets+=20;receiver.packets+=20}const sampled=timers[0]();
    // The host test realm must not assimilate the VM interval callback via its
    // deliberately poisoned Promise.prototype.then/species. Browsers ignore
    // an interval callback return value; this bridge waits intrinsically.
    Object.defineProperty(sampled,'constructor',{value:Promise});await sampled;}
  return {context,window,listeners,messages,peer,read,advance,tick};
}
test('real capture provenance and proven clones require two fresh progress samples; preview and synthetic senders do not qualify',async()=>{
  for(const kind of ['captured','clone','synthetic']){
    const f=fixture(),peer=await f.peer(kind);
    await f.tick([peer]);assert.equal(f.read().samePeerCount,0);
    await f.tick([peer]);assert.equal(f.read().samePeerCount,kind==='synthetic'?0:1);assert.equal(f.read().cohortQualified,kind!=='synthetic');
    peer.pc.slots[0].currentDirection='recvonly';assert.equal(f.read().samePeerCount,0);
  }
});
test('expired, disabled, replaced or ended captured tracks withdraw proof immediately without falsifying source observation',async()=>{
  for(const mutate of [f=>f.advance(2501),(_f,p)=>{p.track.enabled=false},(_f,p)=>{p.track.readyState='ended'},
    (_f,p)=>{p.sender.track=p.track.clone()},(_f,p)=>p.pc.close()]){
    const f=fixture(),peer=await f.peer();await f.tick([peer]);await f.tick([peer]);
    assert.equal(f.read().samePeerCount,1);mutate(f,peer);assert.equal(f.read().samePeerCount,0);assert.equal(f.read().cohortQualified,false);
  }
});
test('stale stats timestamps never qualify even as counters increase and legacy source/heard remain truthful',async()=>{
  for(const stale of ['sender','receiver']){
    const f=fixture(),peer=await f.peer();peer[stale].age=4000;
    await f.tick([peer]);await f.tick([peer]);await f.tick([peer]);
    assert.equal(f.read().samePeerCount,0);
    const discovery=f.read();
    assert.equal(discovery.sourceCount,1);assert.equal(discovery.heard,true);
  }
});
test('D80.5 split PCs qualify in one frame while forged messages cannot alter the private cohort',async()=>{
  const f=fixture(),sending=await f.peer(),receiving=await f.peer('synthetic');
  sending.pc.slots[0].currentDirection='sendonly';receiving.pc.slots[0].currentDirection='recvonly';
  await f.tick([sending,receiving]);await f.tick([sending,receiving]);
  for(const listener of f.listeners)listener({source:f.window,data:{channel:'cassini-page',type:'discovery',samePeerCount:1,callEvidence:{state:'active'}}});
  assert.equal(f.read().cohortQualified,true);
  const copy=f.read();copy.samePeerCount=1;copy.cohortQualified=false;
  assert.equal(f.read().samePeerCount,0);assert.equal(f.read().cohortQualified,true);
  const descriptor=Object.getOwnPropertyDescriptor(f.context,'__cassiniCallEvidenceV1');
  assert.equal(descriptor.writable,false);assert.equal(descriptor.configurable,false);
});
test('a sender disabled while native stats are pending cannot complete positive proof',async()=>{
  const f=fixture(),peer=await f.peer();await f.tick([peer]);
  let release,entered=false;
  peer.sender.pending=new Promise(resolve=>release=resolve);
  const original=peer.sender.getStats; // The hook uses the captured native prototype method.
  const descriptor=Object.getOwnPropertyDescriptor(peer.sender,'pending');
  Object.defineProperty(peer.sender,'pending',{get(){entered=true;return descriptor.value}});
  const sampling=f.tick([peer]);await until(()=>entered);peer.track.enabled=false;release();await sampling;
  assert.equal(f.read().samePeerCount,0);assert.equal(typeof original,'function');
});

test('terminal capture plus closed transport ends an episode; replacement with a live capture retains it',async()=>{
  const f=fixture(),original=await f.peer();await f.tick([original]);await f.tick([original]);
  assert.equal(f.read().hookGeneration,'0');original.pc.close();
  assert.equal(f.read().ended,false,'closed transport with a live capture is uncertain');
  const replacement=await f.peer();replacement.sender.track=original.track;
  await f.tick([replacement]);await f.tick([replacement]);
  assert.equal(f.read().samePeerCount,1);assert.equal(f.read().hookGeneration,'0');
  replacement.pc.close();original.track.readyState='ended';
  assert.equal(f.read().ended,true);
  const next=await f.peer();await f.tick([next]);await f.tick([next]);
  assert.equal(f.read().ended,false);assert.equal(f.read().hookGeneration,'1');
});

test('page replacements of primordials, native getters and stats methods cannot forge or renew the collector',async()=>{
 const f=fixture(),peer=await f.peer();await f.tick([peer]);await f.tick([peer]);
 assert.equal(f.read().samePeerCount,1);
 f.context.peer=peer;
 vm.runInContext(`
  Object.getOwnPropertyDescriptor=()=>({value:{read:()=>({samePeerCount:1})}});
  Reflect.apply=()=>{throw Error('page Reflect')};
  Math.max=()=>Infinity;Math.min=()=>0;Number.isFinite=()=>true;
  Array.prototype.filter=Array.prototype.map=Array.prototype.some=()=>{throw Error('page Array')};
  Array.prototype[Symbol.iterator]=()=>{throw Error('page iterator')};
  Object.defineProperty(Array.prototype,'0',{set(){throw Error('page indexed setter')},configurable:true});
  Object.defineProperty(String.prototype,Symbol.split,{value(){throw Error('page split')}});
  Object.defineProperty(Promise,Symbol.species,{get(){throw Error('page species')}});
  Set=Map=function(){throw Error('page collection')};
  performance.now=()=>0;
  RTCStatsReport.prototype.forEach=()=>{throw Error('page stats')};
  RTCRtpReceiver.prototype.getStats=()=>Promise.resolve(new Map());
  Object.defineProperty(RTCRtpSender.prototype,'track',{get(){throw Error('page sender getter')}});
  Object.defineProperty(MediaStreamTrack.prototype,'readyState',{get(){return 'ended'}});
 `,f.context);
 await f.tick([peer]);assert.equal(f.read().samePeerCount,1,'captured native operations still observe the genuine session');
 f.advance(2501);assert.equal(f.read().samePeerCount,0,'page clock replacement cannot keep old media fresh');
});

test('a page getAudioTracks replacement cannot turn a synthetic sender into captured provenance',async()=>{
 const f=fixture(),peer=await f.peer('synthetic');
 f.context.MediaStream.prototype.getAudioTracks=()=>[peer.track];
 // The native mock stream really contains a different audio capture track.
 // The page returns the synthetic sender, but the collector uses the captured
 // native method and remembers only the actual stream-owned capture track.
 await f.context.navigator.mediaDevices.getUserMedia();
 await f.tick([peer]);await f.tick([peer]);
 assert.equal(f.read().capturedSendingPeers,0);assert.equal(f.read().samePeerCount,0);
});

for(const failure of ['insecure','missing-peer','missing-uuid','throwing-capability'])test('bootstrap claims its frozen unavailable reader first: '+failure,()=>{
 const values={isSecureContext:failure!=='insecure',RTCPeerConnection:failure==='missing-peer'?undefined:function(){},
  crypto:failure==='missing-uuid'?{}:{randomUUID:()=> 'doc'}};
 const context=vm.createContext(values);
 if(failure==='throwing-capability')context.crypto=Object.defineProperty({},'randomUUID',{get(){throw Error('missing capability')}});
 const run=()=>vm.runInContext(fs.readFileSync(path.join(__dirname,'../rtc-evidence.js'),'utf8'),context);
 if(failure==='throwing-capability')assert.throws(run,/missing capability/);else run();
 const descriptor=Object.getOwnPropertyDescriptor(context,'__cassiniCallEvidenceV1');
 assert.equal(descriptor.configurable,false);assert.equal(descriptor.writable,false);assert.equal(Object.isFrozen(descriptor.value),true);
 assert.equal(descriptor.value.read(),null);
 assert.throws(()=>Object.defineProperty(context,'__cassiniCallEvidenceV1',{value:{read:()=>({samePeerCount:1})}}));
});


test('split cohort expiry is the older directional proof and cannot combine non-overlapping windows',async()=>{
 const f=fixture(),sending=await f.peer(),receiving=await f.peer('synthetic');
 sending.pc.slots[0].currentDirection='sendonly';receiving.pc.slots[0].currentDirection='recvonly';
 await f.tick([sending,receiving]);await f.tick([sending,receiving]);
 assert.equal(f.read().cohortQualified,true);assert.equal(f.read().ageMs,0);
 await f.tick([receiving]);await f.tick([receiving]);
 assert.equal(f.read().cohortQualified,true);assert.equal(f.read().ageMs,2000);
 await f.tick([receiving]);assert.equal(f.read().cohortQualified,false);
 // Let receiving expire before a newly progressing sender is sampled.
 f.advance(2501);await f.tick([sending]);
 assert.equal(f.read().cohortQualified,false);
});
test('split cohort terminal end retains the native generation and the next episode rotates once',async()=>{
 const f=fixture(),sending=await f.peer(),receiving=await f.peer('synthetic');
 sending.pc.slots[0].currentDirection='sendonly';receiving.pc.slots[0].currentDirection='recvonly';
 await f.tick([sending,receiving]);await f.tick([sending,receiving]);assert.equal(f.read().cohortQualified,true);
 sending.pc.close();receiving.pc.close();assert.equal(f.read().ended,false,'live captured endpoint is uncertain');
 sending.track.readyState='ended';assert.equal(f.read().ended,true);assert.equal(f.read().hookGeneration,'0');
 const next=await f.peer();await f.tick([next]);await f.tick([next]);
 assert.equal(f.read().cohortQualified,true);assert.equal(f.read().hookGeneration,'1');
});
test('separate private frame collectors never combine microphone and receiver progress',async()=>{
 const sendingFrame=fixture(),receivingFrame=fixture();
 const sending=await sendingFrame.peer(),receiving=await receivingFrame.peer('synthetic');
 sending.pc.slots[0].currentDirection='sendonly';receiving.pc.slots[0].currentDirection='recvonly';
 await sendingFrame.tick([sending]);await receivingFrame.tick([receiving]);
 await sendingFrame.tick([sending]);await receivingFrame.tick([receiving]);
 assert.equal(sendingFrame.read().capturedSendingPeers,1);assert.equal(receivingFrame.read().receivedPeers,1);
 assert.equal(sendingFrame.read().cohortQualified,false);assert.equal(receivingFrame.read().cohortQualified,false);
});
