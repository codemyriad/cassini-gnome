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
    constructor() { this.connectionState='new';this.iceConnectionState='new';this.signalingState='stable';this.senders=[];this.receivers=[]; }
    addEventListener() {}
    getTransceivers(){return this.receivers;}
    getSenders(){return this.senders;}
    close(){this.signalingState='closed';}
  }
  for(const method of ['getStats','createOffer','createAnswer','setLocalDescription','setRemoteDescription','addTrack','addTransceiver']) Peer.prototype[method]=()=>{};
  const messages=[],listeners=[],timers=[],timeouts=new Map();let now=0,id=0;
  const window={RTCPeerConnection:Peer,postMessage:m=>messages.push(m),addEventListener:(_name,fn)=>listeners.push(fn)};
  const context=vm.createContext({window,navigator:{},performance:{now:()=>now},setInterval:fn=>timers.push(fn),
    setTimeout(fn,ms){const key=++id;timeouts.set(key,{fn,at:now+ms});return key;},clearTimeout:key=>timeouts.delete(key)});
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../page-hook.js'),'utf8'),context);
  const discover=()=>listeners.forEach(fn=>fn({source:window,data:{channel:'cassini-control',type:'discover'}}));
  const state=()=>JSON.parse(JSON.stringify(messages.at(-1)));
  const advance=ms=>{now+=ms;for(const [key,timer]of timeouts)if(timer.at<=now){timeouts.delete(key);timer.fn();}};
  async function tick(){advance(1000);await timers[0]();}
  function peer(count=1){const pc=new window.RTCPeerConnection();pc.connectionState='connected';
    pc.receivers=Array.from({length:count},()=>receiver());return pc;}
  return {window,messages,timers,discover,state,tick,peer,advance,timeouts};
}
const audio=()=>({kind:'audio',readyState:'live',enabled:true,muted:true});
function receiver(){const slot={currentDirection:'recvonly',reports:[],calls:0};slot.receiver={track:audio(),getStats:async()=>{slot.calls++;return new Map(slot.reports.map(r=>[r.id,r]));}};return slot;}
const report=(packetsReceived,id='inbound')=>({id,type:'inbound-rtp',kind:'audio',packetsReceived});

for(const vector of cases)test('call evidence: '+vector.name,async()=>{
  const f=fixture();let pc=f.peer(vector.receivers);pc.senders=[{track:audio()}];
  for(const step of vector.steps){
    if(step.handoff){pc.close();pc=f.peer(vector.receivers);}
    if(step.close)pc.close();
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
  pc.receivers[0].reports=[report(1)];await f.tick();assert.equal(f.state().heard,true);
  const calls=pc.receivers[0].calls;await f.tick();assert.equal(pc.receivers[0].calls,calls,'latched evidence needs no more polling');
});

test('only valid inbound audio counters vote; failures and outbound reports do not',async()=>{
  const f=fixture(),pc=f.peer(),slot=pc.receivers[0];
  slot.receiver.getStats=async()=>{throw Error('generated stats failure');};await f.tick();
  slot.receiver.getStats=async()=>new Map(slot.reports.map((r,i)=>[i,r]));
  slot.reports=[{...report(10),type:'outbound-rtp'},{...report(10),kind:'video'},report(-1),report(1.5),report('1')];
  await f.tick();await f.tick();assert.equal(f.state().heard,false);
  slot.reports=[report(10)];await f.tick();slot.reports=[report(9)];await f.tick();assert.equal(f.state().heard,false);
  slot.reports=[report(11)];await f.tick();assert.equal(f.state().heard,true);
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
