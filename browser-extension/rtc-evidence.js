// Document-start native observation. No page messages, DOM, or page-replaced
// globals/prototypes may supply admission facts. No device is opened here.
(() => {
  'use strict';
  // Claim the name before secure-context/capability checks or any setup that
  // can fail. The public function remains an immutable unavailable sentinel
  // until the complete native collector has initialized successfully.
  const define=Object.defineProperty,freeze=Object.freeze,create=Object.create;
  let collector=()=>null;
  const api=create(null);define(api,'read',{value:()=>collector()});freeze(api);
  try{define(globalThis,'__cassiniCallEvidenceV1',{value:api})}catch{return}
  if(globalThis.isSecureContext!==true)return;
  const apply=Reflect.apply, construct=Reflect.construct,
    descriptor=Object.getOwnPropertyDescriptor, assign=Object.assign, setPrototype=Object.setPrototypeOf, own=Object.prototype.hasOwnProperty,
    push=Array.prototype.push, indexOf=String.prototype.indexOf, slice=String.prototype.slice, deleteProperty=Reflect.deleteProperty, species=Symbol.species, numberFinite=Number.isFinite,
    safeInteger=Number.isSafeInteger, NativePromise=Promise, promiseThen=Promise.prototype.then;
  const PC=globalThis.RTCPeerConnection, Sender=globalThis.RTCRtpSender,
    Receiver=globalThis.RTCRtpReceiver, Transceiver=globalThis.RTCRtpTransceiver,
    Track=globalThis.MediaStreamTrack, Stream=globalThis.MediaStream,
    Stats=globalThis.RTCStatsReport;
  if (!PC || typeof globalThis.crypto?.randomUUID!=='function') return;
  const method=(prototype,key)=>descriptor(prototype||{},key)?.value;
  const getter=(prototype,key)=>descriptor(prototype||{},key)?.get;
  const call=(fn,target,args=[])=>apply(fn,target,args);
  // Native promise values must not inherit a page-installed thenable hook:
  // otherwise a page can substitute a synthetic stream or mutate stats before
  // our captured callback runs. These interfaces have no native then member.
  for (const prototype of [Stream?.prototype,Stats?.prototype]) if(prototype)
    define(prototype,'then',{value:undefined});
  // Promise.prototype.then normally consults page-mutable @@species. Give
  // this invocation a private constructor with a fixed species, then restore
  // the API promise's own descriptor without invoking its inherited getter.
  const thenConstructor=create(null);define(thenConstructor,species,{value:NativePromise});freeze(thenConstructor);
  const then=(promise,yes,no)=>{
    const previous=descriptor(promise,'constructor');
    define(promise,'constructor',{value:thenConstructor,configurable:true});
    try{return call(promiseThen,promise,[yes,no])}
    finally{if(previous)define(promise,'constructor',previous);else deleteProperty(promise,'constructor')}
  };
  const list=()=>setPrototype([],null);
  const add=(array,value)=>call(push,array,[value]);
  const get=(fn,target)=>{try{return call(fn,target)}catch{return null}};
  const data=(value,key)=>{const entry=descriptor(value,key);return entry&&call(own,entry,['value'])?entry.value:undefined};
  const trackKind=getter(Track?.prototype,'kind'), trackState=getter(Track?.prototype,'readyState'),
    trackEnabled=getter(Track?.prototype,'enabled'), trackMuted=getter(Track?.prototype,'muted'),
    senderTrack=getter(Sender?.prototype,'track'), receiverTrack=getter(Receiver?.prototype,'track'),
    transceiverSender=getter(Transceiver?.prototype,'sender'), transceiverReceiver=getter(Transceiver?.prototype,'receiver'),
    direction=getter(Transceiver?.prototype,'currentDirection'),
    connection=getter(PC.prototype,'connectionState'), ice=getter(PC.prototype,'iceConnectionState'),
    signaling=getter(PC.prototype,'signalingState');
  const transceivers=method(PC.prototype,'getTransceivers'), senderStats=method(Sender?.prototype,'getStats'),
    receiverStats=method(Receiver?.prototype,'getStats'), statsEach=method(Stats?.prototype,'forEach'),
    streamTracks=method(Stream?.prototype,'getAudioTracks'), clone=method(Track?.prototype,'clone');
  const clock=performance, nativeNow=clock.now, origin=clock.timeOrigin,
    nativeTimeout=setTimeout, nativeClear=clearTimeout, nativeInterval=setInterval,
    locationObject=location, href=getter(locationObject,'href'), uuid=crypto.randomUUID, cryptoObject=crypto;
  const now=()=>call(nativeNow,clock), cancel=id=>call(nativeClear,timerTarget,[id]);
  // Capture the receiver of timer calls too: page replacement of globalThis
  // cannot alter native invocation. Timer methods do not consult that receiver.
  const timerTarget=globalThis;
  const timeout=(fn,ms)=>call(nativeTimeout,timerTarget,[fn,ms]);
  const addressOf=()=>{const value=get(href,locationObject)||'',fragment=call(indexOf,value,['#']);return fragment<0?value:call(slice,value,[0,fragment])};
  const roots=list(), peers=list(), qualified=list();
  let overflow=false, generation=0, revision=0, receivedVersion=0, sentVersion=0,
    address=addressOf(), sampling=false, heard=false;
  const documentGeneration=call(uuid,cryptoObject);
  const root=track=>{for(let i=0;i<roots.length;i++)if(roots[i].track===track)return roots[i].root;return null};
  const rememberRoot=(track,ancestor=track)=>{if(roots.length<1024)add(roots,{track,root:ancestor});else overflow=true};
  const live=track=>get(trackKind,track)==='audio'&&get(trackState,track)==='live';
  const closed=pc=>get(signaling,pc)==='closed'||get(connection,pc)==='closed';
  const connected=pc=>!closed(pc)&&(get(connection,pc)==='connected'||get(ice,pc)==='connected'||get(ice,pc)==='completed');
  const terminal=()=>{
    if(!qualified.length)return false;
    for(let i=0;i<qualified.length;i++){
      const entry=qualified[i];if(!closed(entry.pc))return false;
      for(let j=0;j<entry.tracks.length;j++)if(get(trackState,entry.tracks[j])!=='ended')return false;
    }
    for(let i=0;i<peers.length;i++)if(!closed(peers[i].pc))return false;
    return true;
  };
  const reset=()=>{heard=false;qualified.length=0;for(let i=0;i<peers.length;i++){peers[i].receivers=list();peers[i].senders=list();peers[i].generation=null}};
  const refresh=()=>{
    const next=addressOf();
    if(next!==address){address=next;generation++;reset()}
  };
  const record=pc=>{for(let i=0;i<peers.length;i++)if(peers[i].pc===pc)return peers[i];return null};
  const register=pc=>{
    refresh();if(record(pc))return;
    if(terminal()){generation++;reset()}
    // Retire closed objects before applying the bound. Qualified endpoints keep
    // the minimal terminal references until a native new-session boundary.
    for(let i=peers.length-1;i>=0;i--)if(closed(peers[i].pc)){
      for(let j=i;j<peers.length-1;j++)peers[j]=peers[j+1];peers.length--;
    }
    if(peers.length>=64){overflow=true;return}
    add(peers,{pc,generation,receivers:list(),senders:list(),heard:false});
  };
  const capture=globalThis.navigator.mediaDevices;
  const gum=capture?.getUserMedia;
  if(gum&&streamTracks)capture.getUserMedia=function(){
    const promise=call(gum,this,arguments);
    then(promise,stream=>{
      try{const tracks=call(streamTracks,stream);for(let i=0;i<tracks.length;i++)if(live(tracks[i]))rememberRoot(tracks[i])}catch{}
    },()=>{});
    return promise;
  };
  if(clone)Track.prototype.clone=function(){const track=call(clone,this,arguments),ancestor=root(this);if(ancestor)rememberRoot(track,ancestor);return track};
  const Wrapped=new Proxy(PC,{construct(target,args,newTarget){const pc=construct(target,args,newTarget);register(pc);return pc}});
  globalThis.RTCPeerConnection=Wrapped;
  if(globalThis.webkitRTCPeerConnection===PC)globalThis.webkitRTCPeerConnection=Wrapped;
  const remote=method(PC.prototype,'setRemoteDescription');
  if(remote)PC.prototype.setRemoteDescription=function(){
    const value=call(remote,this,arguments),entry=record(this),stamp=generation;
    if(entry)then(value,()=>{refresh();if(stamp===generation&&!closed(entry.pc)&&entry.generation!==generation){entry.generation=generation;entry.receivers=list();entry.senders=list();entry.heard=false}},()=>{});
    return value;
  };
  const pending=executor=>{const promise=new NativePromise(executor);define(promise,'constructor',{value:NativePromise});return promise};
  const bounded=read=>pending(resolve=>{
    let done=false;
    const finish=value=>{if(done)return;done=true;cancel(timer);resolve(value)};
    const timer=timeout(()=>finish(null),750);
    try{then(read(),finish,()=>finish(null))}catch{finish(null)}
  });
  const find=(entries,object)=>{for(let i=0;i<entries.length;i++)if(entries[i].object===object)return entries[i];return null};
  const samples=reports=>{
    const result=list();
    try{call(statsEach,reports,[report=>{if(result.length<128)add(result,report);else overflow=true}])}catch{return result}
    return result;
  };
  const fresh=report=>{const timestamp=data(report,'timestamp'),age=origin+now()-timestamp;return typeof timestamp==='number'&&numberFinite(timestamp)&&age>=0&&age<=2500};
  const nativeSlots=pc=>{
    const result=list();
    try{const slots=call(transceivers,pc);if(slots.length>64)overflow=true;for(let i=0;i<slots.length&&i<64;i++)add(result,slots[i])}catch{}
    return result;
  };
  const sending=(pc,sender,track,slot)=>connected(pc)&&get(senderTrack,sender)===track&&live(track)&&
    get(trackEnabled,track)===true&&get(trackMuted,track)===false&&root(track)!==null&&
    get(transceiverSender,slot)===sender&&(get(direction,slot)==='sendrecv'||get(direction,slot)==='sendonly');
  const receive=(pc,receiver,track,slot)=>connected(pc)&&get(receiverTrack,receiver)===track&&live(track)&&
    get(transceiverReceiver,slot)===receiver&&(get(direction,slot)==='sendrecv'||get(direction,slot)==='recvonly');
  async function sample(){
    if(sampling)return;sampling=true;refresh();const stamp=generation;
    try{
      const tasks=list();
      for(let i=0;i<peers.length;i++){
        const entry=peers[i];if(entry.generation!==stamp||!connected(entry.pc))continue;
        const slots=nativeSlots(entry.pc);
        for(let j=0;j<slots.length;j++){
          if(tasks.length>=128){overflow=true;break}
          const slot=slots[j],receiver=get(transceiverReceiver,slot),track=get(receiverTrack,receiver);
          if(receive(entry.pc,receiver,track,slot))add(tasks,{entry,slot,object:receiver,track,receive:true,promise:bounded(()=>call(receiverStats,receiver))});
          const sender=get(transceiverSender,slot),sent=get(senderTrack,sender);
          if(sending(entry.pc,sender,sent,slot))add(tasks,{entry,slot,object:sender,track:sent,receive:false,promise:bounded(()=>call(senderStats,sender))});
        }
      }
      // All native calls have started; awaiting our own bounded promises uses
      // the intrinsic Promise path, with an own captured constructor.
      for(let i=0;i<tasks.length;i++){
        const task=tasks[i],report=await task.promise,entry=task.entry;
        if(stamp!==generation||entry.generation!==stamp||!(task.receive?receive:sending)(entry.pc,task.object,task.track,task.slot))continue;
        const entries=task.receive?entry.receivers:entry.senders;
        let state=find(entries,task.object);
        if(state&&state.track!==task.track){state.track=task.track;state.slot=task.slot;state.counts=list();state.at=-Infinity;state.progress=0;state.votes=0}
        if(!state){state={object:task.object,track:task.track,slot:task.slot,counts:list(),at:-Infinity,progress:0,votes:0};if(entries.length<64)add(entries,state);else{overflow=true;continue}}
        const values=samples(report),counts=list();
        for(let j=0;j<values.length;j++){
          const row=values[j],type=data(row,'type'),kind=data(row,'kind')??data(row,'mediaType'),id=data(row,'id'),
            count=data(row,task.receive?'packetsReceived':'packetsSent'),at=data(row,'timestamp');
          if(type!==(task.receive?'inbound-rtp':'outbound-rtp')||kind!=='audio'||typeof id!=='string'||!safeInteger(count)||count<0)continue;
          const currentFresh=fresh(row);
          let previous=null;for(let k=0;k<state.counts.length;k++)if(state.counts[k].id===id)previous=state.counts[k];
          if(previous&&task.receive&&count-previous.count>=10)state.votes++;
          if(previous&&previous.fresh&&currentFresh&&at>previous.at&&at-previous.at<=2500&&count>previous.count){
            state.at=at-origin;state.progress=task.receive?++receivedVersion:++sentVersion;
          }
          add(counts,{id,count,at,fresh:currentFresh});
        }
        state.counts=counts;
      }
      if(stamp===generation)revision++;
    }catch{/* Any unavailable native brand or API withdraws proof. */}
    finally{sampling=false}
  }
  function read(){
    refresh();const instant=now();let sourceCount=0,receivedPeers=0,capturedSendingPeers=0,samePeerCount=0,
      received=0,sent=0,receiveAt=0,sendAt=0,heardNow=false;
    const members=list();
    for(let i=0;i<peers.length;i++){
      const entry=peers[i];if(entry.generation!==generation||!connected(entry.pc))continue;
      let receiving=false,sendingNow=false;
      const slots=nativeSlots(entry.pc),captured=list();
      for(let j=0;j<slots.length;j++){
        const slot=slots[j],receiver=get(transceiverReceiver,slot),track=get(receiverTrack,receiver),sender=get(transceiverSender,slot),out=get(senderTrack,sender);
        if(receive(entry.pc,receiver,track,slot)){
          sourceCount++;const proof=find(entry.receivers,receiver);
          if(proof?.track===track&&proof.votes>=2)entry.heard=true;
          if(proof?.track===track&&instant-proof.at<=2500){receiving=true;if(proof.progress>received){received=proof.progress;receiveAt=proof.at}}
        }
        if(sending(entry.pc,sender,out,slot)){
          const proof=find(entry.senders,sender);
          if(proof?.track===out&&instant-proof.at<=2500){sendingNow=true;if(proof.progress>sent){sent=proof.progress;sendAt=proof.at}add(captured,out)}
        }
      }
      if(entry.heard)heardNow=true;
      if(receiving)receivedPeers++;if(sendingNow)capturedSendingPeers++;
      if(receiving&&sendingNow)samePeerCount++;
      if(receiving||sendingNow)add(members,{pc:entry.pc,tracks:captured});
    }
    // D80.5: all these native peers belong to this one document/frame collector.
    // Never combine observations from another collector, document or relay port.
    const cohortQualified=!overflow&&receivedPeers>0&&capturedSendingPeers>0;
    if(cohortQualified)for(let i=0;i<members.length;i++){
      const member=members[i];let endpoint=null;
      for(let j=0;j<qualified.length;j++)if(qualified[j].pc===member.pc)endpoint=qualified[j];
      // Retain previously observed capture endpoints through mute/replacement
      // until a native terminal/new-session boundary, as in the same-PC path.
      if(endpoint){if(member.tracks.length)endpoint.tracks=member.tracks}
      else if(qualified.length<64)add(qualified,member);
    }
    const older=receiveAt<sendAt?receiveAt:sendAt,age=cohortQualified?instant-older:0;
    return assign(create(null),{version:2,documentGeneration,hookGeneration:''+generation,revision,ageMs:age,cohortQualified,
      sourceCount:sourceCount>64?64:sourceCount,heard:sourceCount>0&&heardNow,
      receivedPeers,capturedSendingPeers,samePeerCount:overflow?0:samePeerCount,
      receivedVersion:received,sentVersion:sent,ended:terminal()});
  }
  call(nativeInterval,timerTarget,[sample,1000]);
  collector=read;
})();
