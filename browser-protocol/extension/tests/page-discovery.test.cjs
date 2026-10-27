const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const path=require('node:path');

function fixture() {
  class Peer {
    constructor() { this.connectionState='new';this.iceConnectionState='new';this.signalingState='stable';this.senders=[];this.receivers=[]; }
    addEventListener() {}
    getTransceivers(){return this.receivers;}
    getSenders(){return this.senders;}
    close(){this.signalingState='closed';}
  }
  for(const method of ['getStats','createOffer','createAnswer','setLocalDescription','setRemoteDescription','addTrack','addTransceiver']) Peer.prototype[method]=()=>{};
  const messages=[],listeners=[],timers=[];
  const window={RTCPeerConnection:Peer,postMessage:m=>messages.push(m),addEventListener:(_name,fn)=>listeners.push(fn)};
  const context=vm.createContext({window,navigator:{},setInterval:fn=>timers.push(fn)});
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../page-hook.js'),'utf8'),context);
  const discover=()=>listeners.forEach(fn=>fn({source:window,data:{channel:'cassini-control',type:'discover'}}));
  const count=()=>messages.at(-1)?.sourceCount;
  return {window,context,messages,timers,discover,count};
}
const audio=()=>({kind:'audio',readyState:'live',enabled:true});

test('connected audio is discovered without capture APIs; preview and video are excluded',()=>{
  const f=fixture(),pc=new f.window.RTCPeerConnection();
  pc.senders=[{track:audio()}];
  f.discover();assert.equal(f.count(),0,'pre-join local preview is not a meeting');
  pc.connectionState='connected';
  pc.receivers=[{currentDirection:'recvonly',receiver:{track:audio()}},{currentDirection:'recvonly',receiver:{track:{kind:'video',readyState:'live'}}}];
  f.discover();assert.equal(f.count(),2);
  pc.senders[0].track.enabled=false;
  f.discover();assert.equal(f.count(),2,'meeting mute does not remove discovery');
  pc.close();f.discover();assert.equal(f.count(),0,'peer close removes meeting');
});

test('discovery polling reports change and explicit requests replay unchanged state',()=>{
  const f=fixture(),pc=new f.window.RTCPeerConnection();
  pc.connectionState='connected';pc.senders=[{track:audio()}];
  f.timers[0]();assert.equal(f.count(),1);
  const before=f.messages.length;f.timers[0]();assert.equal(f.messages.length,before);
  f.discover();assert.equal(f.messages.length,before+1);
  pc.senders[0].track.readyState='ended';f.timers[0]();assert.equal(f.count(),0);
});
