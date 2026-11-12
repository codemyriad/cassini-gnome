const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
const path=require('node:path');
const moduleURL='data:text/javascript;base64,'+Buffer.from('export const capture=options=>globalThis.__cassiniPageCapture(options);').toString('base64');

module.exports = async function fixture(run, settings={}) {
 const messages=[],listeners=[],timers=[],captures=[],media=[];let now=0,uuid=0,contextError=false,nativeFinished=false;
 class Track {constructor(value){Object.assign(this,value)}clone(){return new Track({...this,id:this.id+'-clone'})}}
 class Peer {
  constructor(){this.connectionState='connected';this.iceConnectionState='connected';this.signalingState='stable';this.slots=[];this.senders=[]}
  addEventListener(){}getTransceivers(){return this.slots}getSenders(){return this.senders}getReceivers(){return this.slots.map(s=>s.receiver)}close(){this.signalingState='closed'}
 }
 for(const name of ['getStats','createOffer','createAnswer','setLocalDescription','setRemoteDescription','addTrack','addTransceiver'])Peer.prototype[name]=()=>{};
 Peer.prototype.setRemoteDescription=function(){return this.descriptionTask || Promise.resolve()};
 class Stream {constructor(tracks){this.tracks=tracks}getAudioTracks(){return this.tracks}clone(){return new Stream(this.tracks.map(track=>Object.assign(Object.create(Track.prototype),track,{id:track.id+'-stream-clone'})))}}
 // Minimal WebAudio graph: nodes connect to nodes of their context; the page hook follows it.
 class AudioParam {}
 class AudioNode {
  constructor(context,extra={}){this.context=context;this.links=[];Object.assign(this,extra)}
  connect(target,output=0,input=0){if(!(target instanceof AudioNode||target instanceof AudioParam))throw new TypeError('not a node');this.links.push({target,output,input});return target}
  disconnect(target){if(target!==undefined&&!this.links.some(l=>l.target===target))throw Error('InvalidAccessError');this.links=this.links.filter(l=>target!==undefined&&l.target!==target)}
 }
 class Context {constructor(){if(contextError)throw Error('generated context attach failure')}sampleRate=48000;state='running';async close(){this.state='closed'}
  createMediaStreamSource(mediaStream){return new AudioNode(this,{mediaStream})}
  createMediaElementSource(mediaElement){return new AudioNode(this,{mediaElement})}
  createMediaStreamDestination(){return new AudioNode(this,{stream:new Stream([new Track({id:'webaudio-output',label:'MediaStreamAudioDestinationNode',kind:'audio',readyState:'live',enabled:true,muted:false})])})}
  createGain(){return new AudioNode(this,{gain:new AudioParam()})}
 }
 const control=async data=>{for(const fn of listeners)await fn({isTrusted:true,source:window,data:{channel:'cassini-control',...data}})};
 const window={location:{href:'https://meet.test/call-A'},RTCPeerConnection:Peer,addEventListener:(_n,fn)=>listeners.push(fn),postMessage(message){
  messages.push(message);
  if(settings.deliver){settings.deliver(message);return;}
  if(message.type==='packet')queueMicrotask(()=>control({type:'ack',token:message.token,request:message.request,...(nativeFinished?{error:'Native capture already saved',scope:'command',code:'state'}:{})}));
 }};
 globalThis.__cassiniPageCapture=async options=>{
  const item={...options,stopped:false};captures.push(item);
  if(settings.capture){item.recorder=await settings.capture(options);return item.recorder;}
  options.getSharedContext?.();
  await options.send({type:'track',id:options.id,label:options.label,...(options.labelSource?{labelSource:options.labelSource}:{}),kind:options.kind,sampleRate:48000,channels:1,sourceTrackId:options.sourceTrackId,clockMethod:'roundTrip',clockUncertaintyMs:9,clockClampMs:3});
  return {failed:false,async stop(){item.stopped=true}};
 };
 const ContextClass=settings.Context || Context;
 let nextInput=0;
 const navigator={mediaDevices:{
  async getUserMedia(){return new Stream([new Track({id:`user-mic-${++nextInput}`,label:'Headset Microphone',kind:'audio',readyState:'live',enabled:true,muted:false})])},
  async getDisplayMedia(){return new Stream([new Track({id:`display-audio-${++nextInput}`,label:'Tab audio',kind:'audio',readyState:'live',enabled:true,muted:false})])}
 }};
 const context=vm.createContext({window,navigator,document:{querySelectorAll:()=>media},MediaStream:Stream,MediaStreamTrack:Track,AudioContext:ContextClass,AudioNode,AudioParam,
  crypto:{randomUUID:()=>`id-${++uuid}`},performance:{now:()=>now},setInterval:fn=>(timers.push(fn),timers.length),clearInterval(){},
  setTimeout,clearTimeout,queueMicrotask});
 vm.runInContext(fs.readFileSync(path.join(__dirname,'../../page-hook.js'),'utf8'),context,{importModuleDynamically:vm.constants.USE_MAIN_CONTEXT_DEFAULT_LOADER});
 const peer=new window.RTCPeerConnection();
 function remote(label='Guest',target=peer){
  const slot={currentDirection:'recvonly',packets:0};
  slot.receiver={track:new Track({id:`remote-${peer.slots.length}`,label,kind:'audio',readyState:'live',enabled:true,muted:false}),
   getStats:async()=>new Map([['rtp',{id:'rtp',type:'inbound-rtp',kind:'audio',packetsReceived:slot.packets}]])};
  target.slots.push(slot);return slot;
 }
 const tick=async()=>{now+=1000;await timers[0]();};
 const scan=async()=>{assert.ok(timers[1],'capture scan exists');await timers.at(-1)();};
 let activeToken='recording';
 try {await run({peer,window,navigator,Track,AudioContext:context.AudioContext,remote,tick,scan,captures,messages,cachedPeer:()=>new Peer(),receive(data){if(data.type==="start")activeToken=data.token;return control(data)},render(track,card){const element={srcObject:new Stream([track]),closest:()=>card};media.push(element);return element},nativeStopFirst(){nativeFinished=true},breakContext(){contextError=true},async stop(){await control({type:'stop',token:activeToken})},async start(token='recording',options={}){activeToken=token;await control({type:'start',token,origin:0,moduleURL,workletURL:'',...options})}})}
 finally {await control({type:'stop',token:activeToken});delete globalThis.__cassiniPageCapture;}
}
;

module.exports.moduleURL=moduleURL;
