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
  addEventListener(){}getTransceivers(){return this.slots}getSenders(){return this.senders}close(){this.signalingState='closed'}
 }
 for(const name of ['getStats','createOffer','createAnswer','setLocalDescription','setRemoteDescription','addTrack','addTransceiver'])Peer.prototype[name]=()=>{};
 Peer.prototype.setRemoteDescription=function(){return this.descriptionTask || Promise.resolve()};
 class Stream {constructor(tracks){this.tracks=tracks}getAudioTracks(){return this.tracks}}
 class Context {constructor(){if(contextError)throw Error('generated context attach failure')}sampleRate=48000;state='running';async close(){this.state='closed'}}
 const control=async data=>{for(const fn of listeners)await fn({source:window,data:{channel:'cassini-control',...data}})};
 const window={location:{href:'https://meet.test/call-A'},RTCPeerConnection:Peer,addEventListener:(_n,fn)=>listeners.push(fn),postMessage(message){
  messages.push(message);
  if(settings.deliver){settings.deliver(message);return;}
  if(message.type==='packet')queueMicrotask(()=>control({type:'ack',token:message.token,request:message.request,...(nativeFinished?{error:'Native capture already saved',scope:'command',code:'state'}:{})}));
 }};
 globalThis.__cassiniPageCapture=async options=>{
  const item={...options,stopped:false};captures.push(item);
  if(settings.capture){item.recorder=await settings.capture(options);return item.recorder;}
  options.getSharedContext?.();
  await options.send({type:'track',id:options.id,label:options.label,kind:options.kind,sampleRate:48000,channels:1,sourceTrackId:options.sourceTrackId});
  return {failed:false,async stop(){item.stopped=true}};
 };
 const ContextClass=settings.Context || Context;
 const context=vm.createContext({window,navigator:{},document:{querySelectorAll:()=>media},MediaStream:Stream,MediaStreamTrack:Track,AudioContext:ContextClass,
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
 try {await run({peer,window,remote,tick,scan,captures,messages,receive(data){if(data.type==="start")activeToken=data.token;return control(data)},render(track){media.push({srcObject:new Stream([track])})},nativeStopFirst(){nativeFinished=true},breakContext(){contextError=true},async stop(){await control({type:'stop',token:activeToken})},async start(token='recording',options={}){activeToken=token;await control({type:'start',token,origin:0,moduleURL,workletURL:'',...options})}})}
 finally {await control({type:'stop',token:activeToken});delete globalThis.__cassiniPageCapture;}
}
;

module.exports.moduleURL=moduleURL;
