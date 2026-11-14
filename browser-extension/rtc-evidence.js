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
    Stats=globalThis.RTCStatsReport, AudioNodeClass=globalThis.AudioNode, AudioParamClass=globalThis.AudioParam,
    Context=globalThis.AudioContext||globalThis.webkitAudioContext, SourceNode=globalThis.MediaStreamAudioSourceNode,
    TrackSourceNode=globalThis.MediaStreamTrackAudioSourceNode,
    DestinationNode=globalThis.MediaStreamAudioDestinationNode, NativeWeakMap=globalThis.WeakMap;
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
    streamTracks=method(Stream?.prototype,'getAudioTracks'), clone=method(Track?.prototype,'clone'),
    streamClone=method(Stream?.prototype,'clone'), trackLabel=getter(Track?.prototype,'label');
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
  // Captured-microphone lineage: a getUserMedia audio track (its own root), a
  // proven clone of a lineage track, or the output track of a native WebAudio
  // MediaStreamAudioDestinationNode (node). An output track has a root only
  // while a live captured microphone source reaches that node (see reaches).
  const weakGet=method(NativeWeakMap?.prototype,'get'),weakSet=method(NativeWeakMap?.prototype,'set');
  const lookup=(map,key)=>{try{return call(weakGet,map,[key])}catch{return undefined}};
  const remember=(map,key,value)=>{try{call(weakSet,map,[key,value]);return true}catch{return false}};
  const lineageIndex=NativeWeakMap&&weakGet&&weakSet?new NativeWeakMap():null;
  const lineageOf=track=>{
    if(lineageIndex)return lookup(lineageIndex,track)||null;
    for(let i=0;i<roots.length;i++)if(roots[i].track===track)return roots[i];return null;
  };
  const rememberRoot=(track,ancestor=track,node=null)=>{
    if(lineageOf(track))return;
    if(roots.length>=1024){overflow=true;return}
    const value={track,root:ancestor,node};
    if(lineageIndex&&!remember(lineageIndex,track,value))return;add(roots,value);
  };
  const live=track=>get(trackKind,track)==='audio'&&get(trackState,track)==='live';
  // WebAudio graph, recorded only from native calls that succeeded. A missing
  // edge can only withhold lineage; every recorded edge exists natively.
  const graph=!!(lineageIndex&&AudioNodeClass&&method(AudioNodeClass.prototype,'connect')&&method(AudioNodeClass.prototype,'disconnect'));
  const nodeInputs=getter(AudioNodeClass?.prototype,'numberOfInputs'),nodeOutputs=getter(AudioNodeClass?.prototype,'numberOfOutputs'),
    nodeContext=getter(AudioNodeClass?.prototype,'context'),contextState=getter(globalThis.BaseAudioContext?.prototype,'state'),
    paramValue=getter(AudioParamClass?.prototype,'value'),sourceStream=getter(SourceNode?.prototype,'mediaStream'),
    destinationStream=getter(DestinationNode?.prototype,'stream');
  const sources=graph?new NativeWeakMap():null,destinations=graph?new NativeWeakMap():null,
    inbound=graph?new NativeWeakMap():null,outbound=graph?new NativeWeakMap():null;
  const audioNode=value=>get(nodeInputs,value)!==null;
  const contextOf=node=>get(contextState,get(nodeContext,node));
  // One evaluation shares a memo of output nodes (in progress counts as no
  // lineage, which also cuts cycles) and a total work budget: exhausting it
  // withholds lineage. Every output node is walked at most once.
  const evaluation=()=>({memo:new NativeWeakMap(),budget:4096,depth:0});
  // Per output node: the source tracks that fed it a rooted microphone, for
  // the terminal check (bounded; overflow never ends by ancestry).
  const ancestry=graph?new NativeWeakMap():null;
  const feed=(node,tracks)=>{
    let value=lookup(ancestry,node);
    if(!value){value={tracks:list(),overflow:false};if(!remember(ancestry,node,value))return}
    for(let i=0;i<tracks.length;i++){
      let known=false;for(let j=0;j<value.tracks.length;j++)if(value.tracks[j]===tracks[i]){known=true;break}
      if(known)continue;if(value.tracks.length<16)add(value.tracks,tracks[i]);else value.overflow=true;
    }
  };
  function sourceRoot(source,state){
    let found=null;
    for(let i=0;i<source.tracks.length;i++){
      const track=source.tracks[i];
      if(!live(track)||get(trackEnabled,track)!==true||get(trackMuted,track)!==false)return null;
      const ancestor=lineage(track,state);if(!ancestor)return null;found=found||ancestor;
    }
    return found;
  }
  // Only port 0 carries lineage (output 0 into input 0): channel splitters,
  // mergers and multi-port nodes can route other inputs alone to a port.
  // Every walk visits all rooted sources so nested outputs keep their ancestry.
  function reaches(node,state){
    const known=lookup(state.memo,node);if(known!==undefined)return known;
    remember(state.memo,node,null);
    // Only a running context renders its graph into the output track.
    if(!graph||state.depth>8||contextOf(node)!=='running')return null;
    const queue=list(),seen=new NativeWeakMap();add(queue,node);remember(seen,node,true);
    let found=null;
    walk:for(let i=0;i<queue.length;i++){
      // An exhausted budget stops the walk; what it already proved stays genuine.
      if(--state.budget<0)break;
      const current=queue[i],source=lookup(sources,current);
      if(source){
        state.depth++;let rooted=null;try{rooted=sourceRoot(source,state)}finally{state.depth--}
        if(rooted){found=found||rooted;feed(node,source.tracks)}
        continue;
      }
      const inputs=lookup(inbound,current);if(!inputs)continue;
      for(let j=0;j<inputs.length;j++){
        if(--state.budget<0)break walk;
        const edge=inputs[j];
        if(edge.output!==0||edge.input!==0||lookup(seen,edge.from))continue;
        remember(seen,edge.from,true);add(queue,edge.from);
      }
    }
    remember(state.memo,node,found);
    return found;
  }
  function lineage(track,state){
    const value=lineageOf(track);if(!value)return null;
    return value.root||reaches(value.node,state);
  }
  const root=track=>lineage(track,evaluation());
  // An output ends like a track: natively, when its context closed, or when
  // every source track that ever fed it a rooted microphone has ended (nested
  // outputs recursively). Disconnecting or suspending a graph is not an end.
  // One check memoizes each output node (in progress counts as not ended,
  // which cuts cycles) under a work budget whose exhaustion means not ended.
  const terminalCheck=()=>({memo:new NativeWeakMap(),budget:4096,depth:0});
  const finished=(track,state)=>{
    if(get(trackState,track)==='ended')return true;
    const value=lineageOf(track);if(!value||value.root||state.depth>8||--state.budget<0)return false;
    const known=lookup(state.memo,value.node);if(known!==undefined)return known;
    remember(state.memo,value.node,false);
    let ended=contextOf(value.node)==='closed';
    if(!ended){
      const fed=lookup(ancestry,value.node);
      ended=!!fed&&!fed.overflow&&fed.tracks.length>0;
      state.depth++;
      try{for(let i=0;ended&&i<fed.tracks.length;i++)if(!finished(fed.tracks[i],state))ended=false}
      finally{state.depth--}
    }
    remember(state.memo,value.node,ended);
    return ended;
  };
  const closed=pc=>get(signaling,pc)==='closed'||get(connection,pc)==='closed';
  const connected=pc=>!closed(pc)&&(get(connection,pc)==='connected'||get(ice,pc)==='connected'||get(ice,pc)==='completed');
  const terminal=()=>{
    if(!qualified.length)return false;
    const check=terminalCheck();
    for(let i=0;i<qualified.length;i++){
      const entry=qualified[i];if(!closed(entry.pc))return false;
      for(let j=0;j<entry.tracks.length;j++)if(!finished(entry.tracks[j],check))return false;
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
  if(clone)Track.prototype.clone=function(){const track=call(clone,this,arguments),value=lineageOf(this);if(value)rememberRoot(track,value.root,value.node);return track};
  // MediaStream.clone() clones natively, past the track wrapper. Chrome keeps
  // audio track order; an index pair inherits only with the same native label.
  if(streamClone&&streamTracks&&trackLabel)Stream.prototype.clone=function(){
    const stream=call(streamClone,this,arguments);
    try{
      const from=call(streamTracks,this),to=call(streamTracks,stream);
      if(from.length===to.length)for(let i=0;i<from.length;i++){
        const value=lineageOf(from[i]);
        if(value&&get(trackLabel,from[i])===get(trackLabel,to[i]))rememberRoot(to[i],value.root,value.node);
      }
    }catch{}
    return stream;
  };
  if(graph&&streamTracks){
    const connect=method(AudioNodeClass.prototype,'connect'),disconnect=method(AudioNodeClass.prototype,'disconnect');
    // A source carries lineage only when every audio track its stream held at
    // creation has lineage; display, media-element, received and generated
    // audio never do. The native mediaStream getter names the actual stream.
    const rememberSource=node=>{
      const stream=get(sourceStream,node);if(!stream)return;
      const tracks=call(streamTracks,stream),copy=list();
      for(let i=0;i<tracks.length;i++){if(!lineageOf(tracks[i]))return;add(copy,tracks[i])}
      if(copy.length)call(weakSet,sources,[node,{tracks:copy}]);
    };
    const rememberTrackSource=(node,track)=>{
      if(!node||!track||!live(track)||!lineageOf(track))return;
      const copy=list();add(copy,track);
      call(weakSet,sources,[node,{tracks:copy}]);
    };
    const rememberDestination=node=>{
      const stream=get(destinationStream,node);if(!stream)return;
      const tracks=call(streamTracks,stream);call(weakSet,destinations,[node,true]);
      for(let i=0;i<tracks.length;i++)rememberRoot(tracks[i],null,node);
    };
    const argument=(args,position)=>position<args.length?args[position]:undefined;
    const factory=(prototype,key,remember)=>{
      const native=method(prototype,key);if(!native)return;
      prototype[key]=function(){const node=call(native,this,arguments);try{remember(node)}catch{}return node};
    };
    const wrap=(name,Class,remember)=>{if(Class)globalThis[name]=new Proxy(Class,{construct(target,args,newTarget){const node=construct(target,args,newTarget);try{remember(node)}catch{}return node}})};
    if(sourceStream){factory(Context?.prototype,'createMediaStreamSource',rememberSource);wrap('MediaStreamAudioSourceNode',SourceNode,rememberSource)}
    const createTrackSource=method(Context?.prototype,'createMediaStreamTrackSource');
    if(createTrackSource)Context.prototype.createMediaStreamTrackSource=function(){
      const track=argument(arguments,0),node=call(createTrackSource,this,arguments.length?[track]:[]);
      try{rememberTrackSource(node,track)}catch{}return node;
    };
    if(TrackSourceNode)globalThis.MediaStreamTrackAudioSourceNode=new Proxy(TrackSourceNode,{construct(target,args,newTarget){
      // Read own mediaStreamTrack once and pass a null-prototype options bag so
      // getters or proxies cannot hand native construction a different track.
      const options=argument(args,1);
      if(options&&(typeof options==='object'||typeof options==='function')){
        let track;try{track=data(options,'mediaStreamTrack')}catch{}
        if(track!==undefined){
          const clean=create(null);clean.mediaStreamTrack=track;
          const node=construct(target,[argument(args,0),clean],newTarget);
          try{rememberTrackSource(node,track)}catch{}return node;
        }
      }
      return construct(target,args,newTarget);
    }});
    if(destinationStream){factory(Context?.prototype,'createMediaStreamDestination',rememberDestination);wrap('MediaStreamAudioDestinationNode',DestinationNode,rememberDestination)}
    const edges=(map,node)=>{let value=lookup(map,node);if(!value){value=list();call(weakSet,map,[node,value])}return value};
    // WebIDL unsigned long; anything but a number is unknown (-1).
    // Read only supplied argument slots: an omitted slot must never inherit a
    // page-installed numeric prototype property.
    const index=(args,position,fallback)=>{const value=argument(args,position);return value===undefined?fallback:typeof value==='number'?value>>>0:-1};
    const link=(from,args)=>{
      const to=argument(args,0);if(!audioNode(to))return; // AudioParam modulation never carries lineage.
      const output=index(args,1,0),input=index(args,2,0);if(output<0||input<0)return;
      // Input-less generated/element sources and output-less sinks can never
      // carry captured audio into an output track; leave them unrecorded.
      if(!lookup(sources,from)&&get(nodeInputs,from)===0)return;
      if(!lookup(destinations,to)&&get(nodeOutputs,to)===0)return;
      const outs=edges(outbound,from),ins=edges(inbound,to);
      for(let i=0;i<outs.length;i++)if(outs[i].to===to&&outs[i].output===output&&outs[i].input===input)return;
      if(outs.length>=256||ins.length>=256)return;
      add(outs,{to,output,input});add(ins,{from,output,input});
    };
    const drop=(array,i)=>{for(let j=i;j<array.length-1;j++)array[j]=array[j+1];array.length--};
    // Remove every recorded edge that the native call may have removed;
    // unknown arguments match everything (removing lineage is conservative).
    const unlink=(from,args,all)=>{
      const outs=lookup(outbound,from);if(!outs)return;
      let to=null,output=-1,input=-1;
      if(!all&&args.length>0){
        const first=argument(args,0);
        if(audioNode(first)){to=first;if(args.length>1)output=index(args,1,-1);if(args.length>2)input=index(args,2,-1)}
        else if(get(paramValue,first)!==null)return;
        else output=index(args,0,-1);
      }
      for(let i=outs.length-1;i>=0;i--){
        const edge=outs[i];
        if((to!==null&&edge.to!==to)||(output>=0&&edge.output!==output)||(input>=0&&edge.input!==input))continue;
        drop(outs,i);const ins=lookup(inbound,edge.to);
        if(ins)for(let j=ins.length-1;j>=0;j--)if(ins[j].from===from&&ins[j].output===edge.output&&ins[j].input===edge.input)drop(ins,j);
      }
    };
    AudioNodeClass.prototype.connect=function(){const result=call(connect,this,arguments);try{link(this,arguments)}catch{}return result};
    AudioNodeClass.prototype.disconnect=function(){
      const result=call(disconnect,this,arguments);
      try{unlink(this,arguments,false)}catch{try{unlink(this,arguments,true)}catch{}}
      return result;
    };
  }
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
  // Allow up to 5 ms negative skew from performance.now() timer coarsening vs RTCStatsReport.timestamp.
  const fresh=report=>{const timestamp=data(report,'timestamp'),age=origin+now()-timestamp;return typeof timestamp==='number'&&numberFinite(timestamp)&&age>=-5&&age<=2500};
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
    const older=receiveAt<sendAt?receiveAt:sendAt,rawAge=instant-older,age=cohortQualified?(rawAge<0&&rawAge>=-5?0:rawAge):0;
    return assign(create(null),{version:2,documentGeneration,hookGeneration:''+generation,revision,ageMs:age,cohortQualified,
      sourceCount:sourceCount>64?64:sourceCount,heard:sourceCount>0&&heardNow,
      receivedPeers,capturedSendingPeers,samePeerCount:overflow?0:samePeerCount,
      receivedVersion:received,sentVersion:sent,ended:terminal()});
  }
  call(nativeInterval,timerTarget,[sample,1000]);
  collector=read;
})();
