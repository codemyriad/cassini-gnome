// Synthetic call topology only; no devices, audio context, browser or private dump.
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const path=require('node:path');
const root=path.resolve(__dirname,'../..');
const contract=fs.existsSync(path.join(root,'conformance/control/call-evidence-vectors.json'))?root:path.join(root,'browser-protocol');
const cases=JSON.parse(fs.readFileSync(path.join(contract,'conformance/control/call-evidence-vectors.json')));

function fixture() {
  class Peer {
    constructor() { this.connectionState='new';this.iceConnectionState='new';this.signalingState='stable';this.senders=[];this.receivers=[];this.listeners=new Map(); }
    addEventListener(name,fn) { const list=this.listeners.get(name)||[];list.push(fn);this.listeners.set(name,list); }
    emit(name){for(const fn of this.listeners.get(name)||[])fn();}
    getTransceivers(){return this.receivers;}
    getSenders(){return this.senders;}
    getReceivers(){return this.receivers.map(slot=>slot.receiver);}
    close(){this.signalingState='closed';}
  }
  for(const method of ['getStats','createOffer','createAnswer','setLocalDescription','setRemoteDescription','addTrack','addTransceiver']) Peer.prototype[method]=()=>{};
  Peer.prototype.setRemoteDescription=()=>Promise.resolve();
  const messages=[],listeners=[],timers=[],timeouts=new Map();let now=0,id=0;
  const window={RTCPeerConnection:Peer,postMessage:m=>messages.push(m),addEventListener:(_name,fn)=>listeners.push(fn)};
  const context=vm.createContext({window,navigator:{},performance:{now:()=>now},setInterval:fn=>timers.push(fn),
    setTimeout(fn,ms){const key=++id;timeouts.set(key,{fn,at:now+ms});return key;},clearTimeout:key=>timeouts.delete(key)});
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../page-hook.js'),'utf8'),context);
  const discover=()=>listeners.forEach(fn=>fn({isTrusted:true,source:window,data:{channel:'cassini-control',type:'discover'}}));
  const state=()=>JSON.parse(JSON.stringify(messages.at(-1)));
  const advance=ms=>{now+=ms;for(const [key,timer]of timeouts)if(timer.at<=now){timeouts.delete(key);timer.fn();}};
  async function tick(){advance(1000);await timers[0]();}
  function peer(count=1){const pc=new window.RTCPeerConnection();pc.connectionState='connected';
    pc.receivers=Array.from({length:count},()=>receiver());return pc;}
  return {window,messages,timers,discover,state,tick,peer,advance,timeouts,cachedPeer:()=>new Peer()};
}
const audio=()=>({kind:'audio',readyState:'live',enabled:true,muted:true});
function receiver(){const slot={currentDirection:'recvonly',reports:[],calls:0};slot.receiver={track:audio(),getStats:async()=>{slot.calls++;return new Map(slot.reports.map(r=>[r.id,r]));}};return slot;}
const report=(packetsReceived,id='inbound')=>({id,type:'inbound-rtp',kind:'audio',packetsReceived});

for(const vector of cases)test('call evidence: '+vector.name,async()=>{
  const f=fixture();let pc=f.peer(vector.receivers);pc.senders=[{track:audio()}];
  for(const step of vector.steps){
    if(step.handoff){pc.close();pc=f.peer(vector.receivers);}
    if(step.close)pc.close();
    if(step.endReceivers)for(const slot of pc.receivers)slot.receiver.track.readyState="ended";
    if(step.restoreReceivers)pc.receivers=Array.from({length:vector.receivers},()=>receiver());
    if(step.packets!==undefined)pc.receivers[0].reports=step.packets===null?[]:[report(step.packets)];
    for(let i=0;i<(step.ticks||0);i++)await f.tick();
    for(let i=0;i<(step.snapshots||0);i++)f.discover();
    assert.equal(f.state().sourceCount,step.sourceCount,JSON.stringify(step));
    assert.equal(f.state().heard,step.heard,JSON.stringify(step));
  }
});

test('receiver count excludes preview senders, video, ended and unconnected slots',async()=>{
  const f=fixture(),pc=new f.window.RTCPeerConnection();pc.senders=[{track:audio()}];
  f.discover();assert.equal(f.state().sourceCount,0);
  pc.connectionState='connected';await f.tick();assert.equal(f.state().sourceCount,0);
  pc.receivers=[receiver(),receiver(),receiver()];
  pc.receivers[1].receiver.track.kind='video';pc.receivers[2].receiver.track.readyState='ended';
  await f.tick();assert.equal(f.state().sourceCount,1);assert.equal(f.state().heard,false);
  assert.equal(pc.receivers[0].calls,1);assert.equal(pc.receivers[1].calls,0);
  pc.receivers[0].currentDirection='sendonly';await f.tick();assert.equal(f.state().sourceCount,0);
});

test('discovery polling publishes transitions and explicit snapshots do not sample',async()=>{
  const f=fixture(),pc=f.peer();pc.receivers[0].reports=[report(0)];
  await f.tick();const before=f.messages.length;
  await f.tick();assert.equal(f.messages.length,before);
  f.discover();assert.equal(f.messages.length,before+1);assert.equal(pc.receivers[0].calls,2);
  pc.receivers[0].reports=[report(10)];await f.tick();assert.equal(f.state().heard,false);
  pc.receivers[0].reports=[report(20)];await f.tick();assert.equal(f.state().heard,true);
  const calls=pc.receivers[0].calls;f.discover();assert.equal(pc.receivers[0].calls,calls,'forced snapshots do not poll');
});

test('only valid inbound audio counters vote; failures and outbound reports do not',async()=>{
  const f=fixture(),pc=f.peer(),slot=pc.receivers[0];
  slot.receiver.getStats=async()=>{throw Error('generated stats failure');};await f.tick();
  slot.receiver.getStats=async()=>new Map(slot.reports.map((r,i)=>[i,r]));
  slot.reports=[{...report(10),type:'outbound-rtp'},{...report(10),kind:'video'},report(-1),report(1.5),report('1')];
  await f.tick();await f.tick();assert.equal(f.state().heard,false);
  slot.reports=[report(10)];await f.tick();slot.reports=[report(9)];await f.tick();assert.equal(f.state().heard,false);
  slot.reports=[report(19)];await f.tick();assert.equal(f.state().heard,false);
  slot.reports=[report(29)];await f.tick();assert.equal(f.state().heard,true);
});

test('hung or stale stats cannot stall discovery or latch late evidence',async()=>{
  const f=fixture(),pc=f.peer();let release;
  pc.receivers[0].receiver.getStats=()=>new Promise(resolve=>release=resolve);
  const pending=f.tick();await Promise.resolve();await Promise.resolve();
  f.advance(750);await pending;assert.equal(f.state().heard,false);
  release(new Map([['inbound',report(1)]]));await Promise.resolve();
  pc.receivers[0].receiver.getStats=async()=>new Map([['inbound',report(2)]]);
  await f.tick();assert.equal(f.state().heard,false,'late first sample did not vote');
  pc.close();await f.tick();assert.equal(f.state().sourceCount,0);
});

test('closed peers cannot contribute stats that resolve after a handoff',async()=>{
  const f=fixture(),pc=f.peer();let release;
  pc.receivers[0].receiver.getStats=()=>new Promise(resolve=>release=resolve);
  const pending=f.tick();await Promise.resolve();await Promise.resolve();pc.close();f.peer();
  release(new Map([['inbound',report(1)]]));await pending;
  await f.tick();assert.equal(f.state().heard,false);
});


test('a second tick while stats are pending cannot reuse a late vote',async()=>{
  const f=fixture(),pc=f.peer();pc.receivers[0].reports=[report(0)];await f.tick();
  let release;pc.receivers[0].receiver.getStats=()=>new Promise(resolve=>release=resolve);
  const pending=f.tick();await Promise.resolve();await Promise.resolve();
  await f.tick();release(new Map([['inbound',report(100)]]));await pending;
  assert.equal(f.state().heard,false);
  pc.receivers[0].receiver.getStats=async()=>new Map([['inbound',report(100)]]);
  await f.tick();assert.equal(f.state().heard,false,'late stats did not advance the baseline');
  pc.receivers[0].reports=[report(110)];
  pc.receivers[0].receiver.getStats=async()=>new Map(pc.receivers[0].reports.map(r=>[r.id,r]));
  await f.tick();assert.equal(f.state().heard,true);
});

test('an unproved peer cannot borrow the latch from a still-connected owner that disconnects',async()=>{
  const f=fixture(),old=f.peer();old.receivers[0].reports=[report(0)];await f.tick();
  old.receivers[0].reports=[report(10)];await f.tick();old.receivers[0].reports=[report(20)];await f.tick();
  const next=f.peer();next.receivers[0].reports=[report(0)];await f.tick();assert.equal(f.state().heard,true);
  old.connectionState='disconnected';old.iceConnectionState='disconnected';f.discover();assert.equal(f.state().heard,false);
  next.receivers[0].reports=[report(10)];await f.tick();assert.equal(f.state().heard,true);
});

test('a disconnected owner cannot reclaim a latch after five seconds without fresh evidence',async()=>{
  const f=fixture(),pc=f.peer();pc.receivers[0].reports=[report(0)];await f.tick();
  pc.receivers[0].reports=[report(10)];await f.tick();pc.receivers[0].reports=[report(20)];await f.tick();
  pc.connectionState='disconnected';f.discover();assert.equal(f.state().heard,false);
  f.advance(4999);pc.connectionState='connected';f.discover();assert.equal(f.state().heard,true);
  pc.connectionState='disconnected';f.discover();f.advance(5000);f.discover();
  pc.connectionState='connected';f.discover();assert.equal(f.state().heard,false);
  pc.receivers[0].reports=[report(1000)];await f.tick();assert.equal(f.state().heard,false);
  pc.receivers[0].reports=[report(1010)];await f.tick();assert.equal(f.state().heard,false);
  pc.receivers[0].reports=[report(1020)];await f.tick();assert.equal(f.state().heard,true);
});

for(const [name,afterLastDiscover] of [['at the deadline',250],['after the deadline',300]])
test('owner reconnect '+name+' before the next discover requires fresh evidence',async()=>{
  const f=fixture(),pc=f.peer();pc.receivers[0].reports=[report(0)];await f.tick();
  pc.receivers[0].reports=[report(10)];await f.tick();
  pc.receivers[0].reports=[report(20)];await f.tick();assert.equal(f.state().heard,true);
  f.advance(250);pc.connectionState='disconnected';f.discover();assert.equal(f.state().heard,false);
  f.advance(4750);f.discover();assert.equal(f.state().heard,false);
  // The owner returns before any discover has observed the expired deadline.
  f.advance(afterLastDiscover);pc.connectionState='connected';f.discover();
  assert.equal(f.state().heard,false,'a returning owner cannot erase an expired grace');
  pc.receivers[0].reports=[report(1000)];await f.tick();assert.equal(f.state().heard,false,'new baseline');
  pc.receivers[0].reports=[report(1010)];await f.tick();assert.equal(f.state().heard,false,'one fresh vote');
  pc.receivers[0].reports=[report(1020)];await f.tick();assert.equal(f.state().heard,true,'two fresh votes');
});


for(const observe of ['getReceivers','getSenders','getTransceivers'])test('a connected cached-constructor peer is observed through '+observe+' without lending preview evidence',async()=>{
 const f=fixture(),pc=f.cachedPeer();pc.receivers=[receiver()];pc.senders=[{track:audio()}];pc.receivers[0].reports=[report(0)];
 pc[observe]();await f.tick();assert.equal(f.state().sourceCount,0,'prejoin getter access is not call evidence');
 pc.connectionState='connected';await f.tick();
 assert.equal(f.state().sourceCount,1,'the library getter should make this existing receiver discoverable');
 assert.equal(f.state().heard,false,'discovery alone is not call evidence');
 pc.receivers[0].reports=[report(10)];await f.tick();assert.equal(f.state().heard,false);
 pc.receivers[0].reports=[report(20)];await f.tick();assert.equal(f.state().heard,true);
});


for(const observe of ['getStats','getReceivers','getSenders','getTransceivers'])test('late '+observe+' observation cannot import an unknown old peer into a new call',async()=>{
 const f=fixture();f.window.location={href:'https://meeting.test/A'};f.discover();
 const old=f.cachedPeer();old.connectionState='connected';old.receivers=[receiver()];old.receivers[0].reports=[report(100)];
 f.window.location.href='https://meeting.test/B';f.discover();old[observe]();await f.tick();
 assert.equal(f.state().sourceCount,0);assert.equal(f.state().heard,false);
 old.receivers[0].reports=[report(200)];await f.tick();assert.equal(f.state().sourceCount,0);
 await old.setRemoteDescription({type:'answer',sdp:'Fresh B negotiation'});await Promise.resolve();
 await f.tick();assert.equal(f.state().sourceCount,1);assert.equal(f.state().heard,false);
 old.receivers[0].reports=[report(210)];await f.tick();assert.equal(f.state().heard,false);
 old.receivers[0].reports=[report(220)];await f.tick();assert.equal(f.state().heard,true);
});

for(const how of ['close','connection event','connection prune','signaling prune'])
test('closed peer remains absent through library teardown: '+how,async()=>{
 const f=fixture();f.window.location={href:'https://meeting.test/A'};f.discover();
 f.window.location.href='https://meeting.test/B';f.discover();
 const pc=f.peer();pc.iceConnectionState='connected';
 pc.receivers[0].reports=[report(0)];await f.tick();
 pc.receivers[0].reports=[report(10)];await f.tick();pc.receivers[0].reports=[report(20)];await f.tick();
 assert.equal(f.state().heard,true);
 const counts=[...pc.listeners.values()].map(list=>list.length);
 if(how==='close')pc.close();
 else if(how==='signaling prune')pc.signalingState='closed';
 else {pc.connectionState='closed';if(how==='connection event')pc.emit('connectionstatechange');}
 f.discover();assert.equal(f.state().sourceCount,0,'closed connection cannot use stale connected ICE');
 for(const name of ['getReceivers','getSenders','getTransceivers','getStats','close']){
  pc[name]();f.discover();await f.tick();
  assert.equal(f.state().sourceCount,0,name+' cannot resurrect the source');
  assert.equal(f.state().heard,false);
  assert.deepEqual([...pc.listeners.values()].map(list=>list.length),counts,'no new listeners after '+name);
 }
 // Even a late negotiation or misleading later state cannot undo the tombstone.
 pc.connectionState='connected';pc.signalingState='stable';
 await pc.setRemoteDescription({type:'answer',sdp:'late teardown'});pc.getReceivers();
 pc.receivers[0].reports=[report(100)];await f.tick();
 assert.equal(f.state().sourceCount,0);assert.equal(f.state().heard,false);
 assert.deepEqual([...pc.listeners.values()].map(list=>list.length),counts);
});

test('a closed cached-constructor peer is never registered',async()=>{
 const f=fixture(),pc=f.cachedPeer();pc.receivers=[receiver()];pc.connectionState='closed';pc.iceConnectionState='connected';
 for(const name of ['getReceivers','getSenders','getTransceivers','getStats','close'])pc[name]();
 await f.tick();assert.equal(f.state().sourceCount,0);assert.equal(pc.listeners.size,0);
});

test('internal discovery and remote-description adoption use original getters',async()=>{
 const f=fixture();f.window.location={href:'https://meeting.test/A'};f.discover();
 f.window.location.href='https://meeting.test/B';f.discover();
 const pc=f.cachedPeer();pc.connectionState='connected';pc.receivers=[receiver()];pc.getReceivers();
 pc.getTransceivers=()=>{throw Error('library getter must not be called internally');};
 await pc.setRemoteDescription({type:'answer',sdp:'B'});await f.tick();
 assert.equal(f.state().sourceCount,1,'fresh B negotiation adopts the receiver through original getter');
 pc.close();await f.tick();assert.equal(f.state().sourceCount,0);
});
