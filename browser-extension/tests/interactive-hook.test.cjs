// Native media is modeled here; the CI study runs the packaged hook on real RTC.
const {test}=require('node:test');const assert=require('node:assert/strict');
const vm=require('node:vm'),fs=require('node:fs'),path=require('node:path');
const {until}=require('./support/app-control-harness.cjs');
function fixture(){
  let now=0,stateReads=0;const origin=1000000,timers=[],listeners=[],messages=[];
  const mapEach=Map.prototype.forEach;
  class Report extends Map {forEach(fn){return Reflect.apply(mapEach,this,[fn])}}
  class Track {
    #kind='audio';#state='live';#enabled=true;#muted=false;#label;constructor(label='Fixture microphone'){this.#label=label}
    get kind(){return this.#kind}get readyState(){stateReads++;return this.#state}set readyState(value){this.#state=value}
    get enabled(){return this.#enabled}set enabled(value){this.#enabled=value}
    get muted(){return this.#muted}set muted(value){this.#muted=value}get label(){return this.#label}
    clone(){return new Track(this.#label)}
  }
  class Stream {
    #tracks;constructor(...tracks){this.#tracks=tracks}getAudioTracks(){return [...this.#tracks]}
    clone(){return new Stream(...this.#tracks.map(track=>new Track(track.label)))}
  }
  // Native WebAudio is modeled with brand-checked getters, set-like
  // connections and InvalidAccessError for a missing disconnect target.
  class BaseAudioContext {#state='running';get state(){return this.#state}set state(value){this.#state=value}}
  class AudioParam {#value=1;get value(){return this.#value}}
  class AudioNode {
    #context;#inputs;#outputs;#links=[];constructor(context,inputs,outputs){this.#context=context;this.#inputs=inputs;this.#outputs=outputs}
    get context(){return this.#context}get numberOfInputs(){return this.#inputs}get numberOfOutputs(){return this.#outputs}
    connect(target,output=0,input=0){
      output=Number(output)>>>0;input=Number(input)>>>0;
      if(!(target instanceof AudioNode||target instanceof AudioParam))throw new TypeError('not an AudioNode or AudioParam');
      if(target instanceof AudioNode&&target.context!==this.#context)throw Error('InvalidAccessError');
      if(!this.#links.some(l=>l.target===target&&l.output===output&&l.input===input))this.#links.push({target,output,input});
      return target;
    }
    disconnect(...args){
      const [first]=args,output=Number(args[1])>>>0,input=Number(args[2])>>>0;
      const match=args.length===0?()=>true:!(first instanceof AudioNode||first instanceof AudioParam)?l=>l.output===Number(first)>>>0:
        l=>l.target===first&&(args.length<2||l.output===output)&&(args.length<3||l.input===input);
      if(args.length&&!this.#links.some(match))throw Error('InvalidAccessError');
      this.#links=this.#links.filter(l=>!match(l));
    }
  }
  class MediaStreamAudioSourceNode extends AudioNode {#stream;constructor(context,{mediaStream}){super(context,0,1);this.#stream=mediaStream}get mediaStream(){return this.#stream}}
  class MediaStreamTrackAudioSourceNode extends AudioNode {constructor(context,{mediaStreamTrack}){super(context,0,1)}}
  class MediaStreamAudioDestinationNode extends AudioNode {#stream=new Stream(new Track('MediaStreamAudioDestinationNode'));constructor(context){super(context,1,0)}get stream(){return this.#stream}}
  class GainNode extends AudioNode {constructor(context){super(context,1,1);this.gain=new AudioParam()}}
  class AudioContext extends BaseAudioContext {
    constructor(){super();this.destination=new AudioNode(this,1,0)}
    createMediaStreamSource(mediaStream){return new MediaStreamAudioSourceNode(this,{mediaStream})}
    createMediaStreamTrackSource(mediaStreamTrack){return new MediaStreamTrackAudioSourceNode(this,{mediaStreamTrack})}
    createMediaStreamDestination(){return new MediaStreamAudioDestinationNode(this)}
    createGain(){return new GainNode(this)}createOscillator(){return new AudioNode(this,0,1)}createMediaElementSource(){return new AudioNode(this,0,1)}
    createChannelMerger(inputs=6){return new AudioNode(this,inputs,1)}createChannelSplitter(outputs=6){return new AudioNode(this,1,outputs)}
    async close(){this.state='closed'}
  }
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
    MediaStreamTrack:Track,MediaStream:Stream,RTCStatsReport:Report,BaseAudioContext,AudioContext,AudioNode,AudioParam,
    MediaStreamAudioSourceNode,MediaStreamTrackAudioSourceNode,MediaStreamAudioDestinationNode,
    navigator:{mediaDevices:{async getUserMedia(){return new Stream(new Track())},async getDisplayMedia(){return new Stream(new Track('Shared screen audio'))}}},
    crypto:{randomUUID:()=> 'hook-document'},performance:{timeOrigin:origin,now:()=>now},
    setInterval:fn=>timers.push(fn),setTimeout,clearTimeout});
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../rtc-evidence.js'),'utf8'),context);
  async function peer(kind='captured'){
    let track=kind==='synthetic'?new Track():kind instanceof Track?kind:(await context.navigator.mediaDevices.getUserMedia()).getAudioTracks()[0];
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
  return {context,window,listeners,messages,peer,read,advance,tick,Track,stateReads:()=>stateReads};
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

// WebAudio mic processing (Microsoft Teams: getUserMedia -> source -> nodes ->
// MediaStreamDestination -> RTCRtpSender) keeps the captured-microphone lineage.
async function graphPeer(f,build){
 const audio=new f.context.AudioContext(),mic=await f.context.navigator.mediaDevices.getUserMedia();
 const output=audio.createMediaStreamDestination(),built=await build({audio,mic,output,f});
 const peer=await f.peer(output.stream.getAudioTracks()[0]);
 return {...built,audio,mic,output,peer};
}
const qualifies=async(f,peer)=>{await f.tick([peer]);await f.tick([peer]);return f.read().cohortQualified};
test('a microphone processed through WebAudio into a MediaStreamDestination qualifies as captured provenance',async()=>{
 const f=fixture(),g=await graphPeer(f,({audio,mic,output})=>{
  const source=audio.createMediaStreamSource(mic),gain=audio.createGain();
  source.connect(gain).connect(output);return {source,gain};
 });
 assert.equal(await qualifies(f,g.peer),true);assert.equal(f.read().capturedSendingPeers,1);
 g.mic.getAudioTracks()[0].enabled=false;
 assert.equal(f.read().capturedSendingPeers,0,'a disabled microphone withdraws the processed track at once');
 g.mic.getAudioTracks()[0].enabled=true;assert.equal(await qualifies(f,g.peer),true);
 g.gain.disconnect(g.output);
 assert.equal(f.read().capturedSendingPeers,0,'disconnecting the path removes lineage');
 assert.equal(f.read().cohortQualified,false);
 g.gain.connect(g.output);assert.equal(await qualifies(f,g.peer),true,'reconnecting restores it');
 g.source.disconnect();assert.equal(f.read().capturedSendingPeers,0,'disconnect() without arguments removes every edge');
});
test('display, media-element, received and generated audio routed through WebAudio never qualify, even when constructed directly',async()=>{
 for(const kind of ['display','element','oscillator','received','constructor-display','param-modulation']){
  const f=fixture(),g=await graphPeer(f,async({audio,mic,output})=>{
   let input;
   if(kind==='display')input=audio.createMediaStreamSource(await f.context.navigator.mediaDevices.getDisplayMedia());
   if(kind==='constructor-display')input=new f.context.MediaStreamAudioSourceNode(audio,{mediaStream:await f.context.navigator.mediaDevices.getDisplayMedia()});
   if(kind==='element')input=audio.createMediaElementSource({});
   if(kind==='oscillator')input=audio.createOscillator();
   if(kind==='received')input=audio.createMediaStreamSource(new f.context.MediaStream(new f.Track('Remote participant')));
   if(kind==='param-modulation'){
    // A microphone modulating a gain parameter does not make the generated
    // signal through that gain a captured microphone.
    const tone=audio.createOscillator(),gain=audio.createGain();
    audio.createMediaStreamSource(mic).connect(gain.gain);tone.connect(gain);input=gain;
   }
   input.connect(output);
  });
  assert.equal(await qualifies(f,g.peer),false,kind);assert.equal(f.read().capturedSendingPeers,0,kind);
 }
});
test('a mixed WebAudio output qualifies through its microphone, never through the shared-screen input alone',async()=>{
 const f=fixture(),g=await graphPeer(f,async({audio,mic,output})=>{
  const microphone=audio.createMediaStreamSource(mic),screen=audio.createMediaStreamSource(await f.context.navigator.mediaDevices.getDisplayMedia());
  const mixer=audio.createGain();microphone.connect(mixer);screen.connect(mixer);mixer.connect(output);return {microphone};
 });
 assert.equal(await qualifies(f,g.peer),true);
 g.microphone.disconnect();
 assert.equal(f.read().capturedSendingPeers,0,'with the microphone gone, the remaining shared-screen audio has no lineage');
});
test('failed native connects and disconnects never change lineage; unknown disconnect arguments remove it',async()=>{
 const f=fixture(),g=await graphPeer(f,({audio,mic,output})=>{
  const source=audio.createMediaStreamSource(mic),gain=audio.createGain(),other=audio.createGain();
  assert.throws(()=>source.connect(new f.context.AudioContext().createGain()));
  source.connect(gain);gain.connect(output);return {source,gain,other};
 });
 assert.equal(await qualifies(f,g.peer),true);
 assert.throws(()=>g.gain.disconnect(g.other),/InvalidAccessError/);
 assert.equal(f.read().capturedSendingPeers,1,'a throwing native disconnect leaves the graph as it was');
 g.gain.disconnect(g.output,0,0);assert.equal(f.read().capturedSendingPeers,0,'exact disconnect matches the default indices');
 g.gain.connect(g.output);assert.equal(await qualifies(f,g.peer),true);
 const weird={valueOf(){return 0}};
 g.gain.disconnect(g.output,weird);assert.equal(f.read().capturedSendingPeers,0,'non-number indices are treated as a wildcard');
});
test('WebAudio lineage survives chained contexts, constructors, clones and MediaStream.clone(), but not a closed or suspended context',async()=>{
 const f=fixture(),audio=new f.context.AudioContext(),second=new f.context.AudioContext();
 const mic=await f.context.navigator.mediaDevices.getUserMedia();
 const cloned=mic.clone();assert.notEqual(cloned.getAudioTracks()[0],mic.getAudioTracks()[0]);
 const first=new f.context.MediaStreamAudioDestinationNode(audio);
 new f.context.MediaStreamAudioSourceNode(audio,{mediaStream:cloned}).connect(first);
 const output=second.createMediaStreamDestination();
 second.createMediaStreamSource(first.stream.clone()).connect(output);
 const peer=await f.peer(output.stream.getAudioTracks()[0].clone());
 assert.equal(await qualifies(f,peer),true);
 audio.state='suspended';assert.equal(f.read().capturedSendingPeers,0,'a suspended context renders no microphone');
 audio.state='running';assert.equal(await qualifies(f,peer),true);
 await audio.close();assert.equal(f.read().capturedSendingPeers,0,'a closed context withdraws lineage');
});
test('MediaStream.clone() of a captured stream keeps lineage; a clone of a synthetic stream does not',async()=>{
 const f=fixture(),mic=await f.context.navigator.mediaDevices.getUserMedia();
 const captured=await f.peer(mic.clone().getAudioTracks()[0]);
 assert.equal(await qualifies(f,captured),true);
 const g=fixture(),synthetic=await g.peer(new g.context.MediaStream(new g.Track()).clone().getAudioTracks()[0]);
 assert.equal(await qualifies(g,synthetic),false);
});
test('a WebAudio call ends natively when its microphone stops and its peers close, then the next episode rotates',async()=>{
 const f=fixture(),g=await graphPeer(f,({audio,mic,output})=>{audio.createMediaStreamSource(mic).connect(output)});
 assert.equal(await qualifies(f,g.peer),true);
 g.peer.pc.close();assert.equal(f.read().ended,false,'a live microphone still feeds the output track');
 g.mic.getAudioTracks()[0].readyState='ended';
 assert.equal(f.read().ended,true,'the output track no longer carries a live captured microphone');
 const next=await graphPeer(f,({audio,mic,output})=>{audio.createMediaStreamSource(mic).connect(output)});
 assert.equal(await qualifies(f,next.peer),true);assert.equal(f.read().hookGeneration,'1');
});

test('a page numeric prototype property cannot turn an omitted connect index into a stale edge',async()=>{
 const f=fixture(),g=await graphPeer(f,({audio,mic,output})=>({source:audio.createMediaStreamSource(mic)}));
 vm.runInContext('Object.prototype[1]=1;Object.prototype[2]=1;Array.prototype[1]=1',f.context);
 g.source.connect(g.output);assert.equal(await qualifies(f,g.peer),true);
 g.source.disconnect(g.output,0);
 assert.equal(f.read().capturedSendingPeers,0,'the native output-0 edge is removed, not a phantom output-1 edge');
});
test('channel routing: a splitter port fed only by generated audio has no lineage; the microphone port has',async()=>{
 for(const [port,expected] of [[1,false],[0,true]]){
  const f=fixture(),g=await graphPeer(f,({audio,mic,output})=>{
   const merger=audio.createChannelMerger(2),splitter=audio.createChannelSplitter(2);
   audio.createMediaStreamSource(mic).connect(merger,0,0);audio.createOscillator().connect(merger,0,1);
   merger.connect(splitter);splitter.connect(output,port);
  });
  assert.equal(await qualifies(f,g.peer),expected,'splitter output '+port);
 }
});
test('disconnecting a qualified graph after its peers close is not a call end; only an ended microphone is',async()=>{
 const f=fixture(),g=await graphPeer(f,({audio,mic,output})=>{const source=audio.createMediaStreamSource(mic);source.connect(output);return {source}});
 assert.equal(await qualifies(f,g.peer),true);
 g.peer.pc.close();g.source.disconnect();g.audio.state='suspended';
 assert.equal(f.read().ended,false,'teardown of a graph is not proof that the microphone ended');
 g.mic.getAudioTracks()[0].readyState='ended';assert.equal(f.read().ended,true);
});
test('nested WebAudio outputs are walked once per read: work grows linearly with fan-out',async()=>{
 // Five chained contexts, each with `width` sources made from the previous
 // output. The base microphone is disabled, so every path must be explored.
 const work=async width=>{
  const f=fixture(),mic=await f.context.navigator.mediaDevices.getUserMedia();
  let stream=mic;
  for(let level=0;level<5;level++){
   const audio=new f.context.AudioContext(),output=audio.createMediaStreamDestination();
   for(let i=0;i<width;i++)audio.createMediaStreamSource(stream).connect(output);
   stream=output.stream;
  }
  const peer=await f.peer(stream.getAudioTracks()[0]);
  mic.getAudioTracks()[0].enabled=false;
  const before=f.stateReads();assert.equal(f.read().capturedSendingPeers,0);
  return f.stateReads()-before;
 };
 const small=await work(4),large=await work(8);
 assert.ok(large<=small*3,`work ${small} at width 4 and ${large} at width 8 must scale linearly, not exponentially`);
});

test('a chained WebAudio call ends when its microphone stops and its peers close, then the next episode rotates',async()=>{
 const f=fixture(),mic=await f.context.navigator.mediaDevices.getUserMedia();
 const first=new f.context.AudioContext(),inner=first.createMediaStreamDestination();first.createMediaStreamSource(mic).connect(inner);
 const second=new f.context.AudioContext(),outer=second.createMediaStreamDestination();second.createMediaStreamSource(inner.stream).connect(outer);
 const peer=await f.peer(outer.stream.getAudioTracks()[0]);
 assert.equal(await qualifies(f,peer),true);
 peer.pc.close();assert.equal(f.read().ended,false);
 mic.getAudioTracks()[0].readyState='ended';
 assert.equal(f.read().ended,true,'the nested output inherits its microphone ancestry');
 const next=await graphPeer(f,({audio,mic,output})=>{audio.createMediaStreamSource(mic).connect(output)});
 assert.equal(await qualifies(f,next.peer),true);assert.equal(f.read().hookGeneration,'1');
});

test('the terminal check walks each nested output once: teardown work grows linearly with fan-out',async()=>{
 const work=async width=>{
  const f=fixture(),mic=await f.context.navigator.mediaDevices.getUserMedia();
  let stream=mic;
  for(let level=0;level<6;level++){
   const audio=new f.context.AudioContext(),output=audio.createMediaStreamDestination();
   // Distinct stream clones of the previous output (the microphone itself at level 0,
   // whose ended track is the affirmative end).
   for(let i=0;i<width;i++)audio.createMediaStreamSource(level?stream.clone():stream).connect(output);
   stream=output.stream;
  }
  const peer=await f.peer(stream.getAudioTracks()[0]);
  assert.equal(await qualifies(f,peer),true);
  peer.pc.close();mic.getAudioTracks()[0].readyState='ended';
  const before=f.stateReads();assert.equal(f.read().ended,true);
  return f.stateReads()-before;
 };
 const small=await work(4),large=await work(8);
 assert.ok(large<=small*3,`teardown work ${small} at width 4 and ${large} at width 8 must scale linearly`);
});

test('MediaStreamTrackAudioSourceNode and createMediaStreamTrackSource keep captured mic lineage and reject non-mic or getter-swapped tracks',async()=>{
 const f=fixture(),g=await graphPeer(f,({audio,mic,output})=>{
  audio.createMediaStreamTrackSource(mic.getAudioTracks()[0]).connect(output);
 });
 assert.equal(await qualifies(f,g.peer),true);
 const fCtor=fixture(),gCtor=await graphPeer(fCtor,({audio,mic,output})=>{
  new fCtor.context.MediaStreamTrackAudioSourceNode(audio,{mediaStreamTrack:mic.getAudioTracks()[0]}).connect(output);
 });
 assert.equal(await qualifies(fCtor,gCtor.peer),true);
 const fNeg=fixture(),gNeg=await graphPeer(fNeg,async({audio,output})=>{
  const display=await fNeg.context.navigator.mediaDevices.getDisplayMedia();
  audio.createMediaStreamTrackSource(display.getAudioTracks()[0]).connect(output);
 });
 assert.equal(await qualifies(fNeg,gNeg.peer),false);
 const fSwap=fixture(),gSwap=await graphPeer(fSwap,async({audio,mic,output})=>{
  const display=await fSwap.context.navigator.mediaDevices.getDisplayMedia();
  const options={};
  Object.defineProperty(options,'mediaStreamTrack',{configurable:true,get(){
   Object.defineProperty(options,'mediaStreamTrack',{value:mic.getAudioTracks()[0],configurable:true});
   return display.getAudioTracks()[0];
  }});
  new fSwap.context.MediaStreamTrackAudioSourceNode(audio,options).connect(output);
 });
 assert.equal(await qualifies(fSwap,gSwap.peer),false);
});

test('sub-millisecond negative timestamp skew is tolerated and clamped while larger future timestamps are rejected',async()=>{
 const f=fixture(),p=await f.peer('captured');
 p.sender.age=-1;p.receiver.age=-1;
 assert.equal(await qualifies(f,p),true);
 assert.equal(f.read().ageMs,0);
 const fBad=fixture(),pBad=await fBad.peer('captured');
 pBad.sender.age=-10;pBad.receiver.age=-10;
 assert.equal(await qualifies(fBad,pBad),false);
});
