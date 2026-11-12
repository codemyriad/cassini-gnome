// D52 production page-hook; generated peers only, no desktop or real devices.
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fixture=require('./support/page-capture-harness.cjs');

test('remote span waits for a connected peer and a positive packet delta',()=>fixture(async f=>{
 const remote=f.remote();f.peer.connectionState='new';f.peer.iceConnectionState='new';
 await f.tick();await f.start();assert.equal(f.captures.length,0);
 f.peer.connectionState='connected';await f.tick();await f.scan();assert.equal(f.captures.length,0,'first appearance establishes only a baseline');
 remote.packets=1;await f.tick();await f.scan();assert.equal(f.captures.length,1);
 assert.ok(f.captures[0].sourceTrackId);
}));
test('the page relays capture clock facts as declared and never names the document itself',()=>fixture(async f=>{
 // D61: the background stamps documentGeneration from the sending frame; the page adds none.
 const remote=f.remote();await f.tick();remote.packets=10;await f.tick();await f.start();
 const track=f.messages.find(m=>m.type==='packet'&&m.packet.type==='track').packet;
 assert.deepEqual([track.clockMethod,track.clockUncertaintyMs,track.clockClampMs],['roundTrip',9,3]);
 assert.equal('documentGeneration' in track,false);
}));
test('equal labels have different identities; a receiver replacement retains its root identity',()=>fixture(async f=>{
 const a=f.remote(),b=f.remote();await f.tick();a.packets=b.packets=10;await f.tick();await f.start();
 assert.equal(f.captures.length,2);assert.notEqual(f.captures[0].sourceTrackId,f.captures[1].sourceTrackId);
 const first=f.captures[0];a.receiver.track={...a.receiver.track,id:'replacement'};
 await f.scan();assert.equal(first.stopped,true);assert.equal(f.captures.length,2,'replacement requires its own packet evidence');
 a.packets=20;await f.tick();await f.scan();assert.equal(f.captures.length,3);
 assert.equal(f.captures[2].sourceTrackId,first.sourceTrackId);assert.notEqual(f.captures[2].id,first.id);
 assert.equal(f.captures[1].stopped,false,'the other participant continues');
}));
test('sender replace/mute/restore keeps one source key without overlapping spans',()=>fixture(async f=>{
 const sender={track:{id:'mic-a',label:'Microphone',kind:'audio',readyState:'live',enabled:true,muted:false},getParameters:()=>({encodings:[]})};
 f.peer.senders=[sender];await f.start();const first=f.captures[0];assert.ok(first);
 sender.track=null;await f.scan();assert.equal(f.captures.length,1);assert.equal(first.stopped,false);
 sender.track={id:'mic-b',label:'Microphone',kind:'audio',readyState:'live',enabled:true,muted:false};await f.scan();
 assert.equal(first.stopped,true);assert.equal(f.captures.length,2);assert.equal(f.captures[1].sourceTrackId,first.sourceTrackId);
}));

test('an in-page call switch retires A and never opens a B source under A token',()=>fixture(async f=>{
 const a=f.remote();await f.tick();a.packets=10;await f.tick();await f.start('A');const first=f.captures[0];
 f.window.location.href='https://meet.test/call-B';
 const next=new f.window.RTCPeerConnection(),b=f.remote('Guest',next);
 await f.tick();b.packets=10;await f.tick();await f.scan();
 assert.equal(first.stopped,true);assert.equal(f.captures.length,1,'old token cannot admit the new call');
 await f.stop();await f.start('B');assert.equal(f.captures.length,2);
 assert.notEqual(f.captures[1].sourceTrackId,first.sourceTrackId);
 assert.deepEqual(f.messages.filter(m=>m.type==='packet'&&m.packet.type==='track').map(m=>m.token),['A','B']);
 assert.equal(f.peer.signalingState,'stable','old A peer deliberately closes late');
}));


test('a reused peer needs a fresh applied remote description and fresh RTP in the new call',()=>fixture(async f=>{
 const a=f.remote();await f.tick();a.packets=10;await f.tick();await f.start('A');
 const first=f.captures[0];f.window.location.href='https://meet.test/call-B';
 await f.tick();await f.scan();assert.equal(first.stopped,true);
 await f.stop();await f.start('B');a.packets=20;await f.tick();await f.scan();
 assert.equal(f.captures.length,1,'old connected peer stays fenced without new negotiation');
 await f.peer.setRemoteDescription({type:'answer',sdp:'B'});await f.tick();await f.scan();
 assert.equal(f.captures.length,1,'a fresh stats baseline alone is not audio evidence');
 a.packets=30;await f.tick();await f.scan();
 assert.equal(f.captures.length,2,'a reused receiver can record the new call');
 assert.equal(f.captures[1].sourceTrackId,first.sourceTrackId);
 assert.notEqual(f.captures[1].id,first.id);
 assert.deepEqual(f.messages.filter(m=>m.type==='packet'&&m.packet.type==='track').map(m=>m.token),['A','B']);
}));

test('an old remote-description completion cannot rebind an old peer to the new call',()=>fixture(async f=>{
 const a=f.remote();await f.tick();a.packets=10;await f.tick();await f.start('A');
 let release;f.peer.descriptionTask=new Promise(resolve=>release=resolve);
 const pending=f.peer.setRemoteDescription({type:'answer',sdp:'old-A'});
 f.window.location.href='https://meet.test/call-B';await f.tick();release();await pending;
 await f.scan();await f.stop();await f.start('B');a.packets=20;await f.tick();await f.scan();
 assert.equal(f.captures.length,1,'delayed old negotiation remains fenced');
 f.peer.descriptionTask=Promise.reject(new Error('new negotiation failed'));
 await assert.rejects(f.peer.setRemoteDescription({type:'answer',sdp:'failed-B'}));
 a.packets=30;await f.tick();await f.scan();
 assert.equal(f.captures.length,1,'failed new negotiation cannot rebind the peer either');
}));

test('a late old peer sender is not recorded by the new call',()=>fixture(async f=>{
 f.peer.senders=[{track:{id:'old-mic',kind:'audio',readyState:'live',enabled:true,muted:false},getParameters:()=>({encodings:[]})}];
 await f.start('A');assert.equal(f.captures.length,1);
 f.window.location.href='https://meet.test/call-B';await f.tick();await f.scan();
 await f.stop();await f.start('B');
 assert.equal(f.captures.length,1,'old call sender cannot leak into B');
}));

test('discovery is acknowledged before an AudioContext attach failure and the failure is sent before Stop',()=>fixture(async f=>{
 const remote=f.remote();await f.tick();remote.packets=10;await f.tick();f.breakContext();
 await f.start('failed-source',{sourceOutcomes:true});await f.stop();
 const updates=f.messages.filter(m=>m.type==='packet'&&m.packet.type==='sources');
 assert.equal(updates.length,2);assert.equal(updates[0].packet.discoveredSources[0].failed,false);
 assert.equal(updates[1].packet.discoveredSources[0].failed,true);
 assert.equal(updates[0].packet.discoveredSources[0].sourceTrackId,updates[1].packet.discoveredSources[0].sourceTrackId);
 assert.equal(f.messages.filter(m=>m.type==='packet'&&m.packet.type==='track').length,0,'no WAV/track was opened');
 assert.ok(f.messages.indexOf(updates[1])<f.messages.findIndex(m=>m.type==='stopped'));
 assert.equal(f.messages.find(m=>m.type==='stopped').discoveredSources,undefined,'Stop is not the only discovery carrier');
}));

test('257 discovered sources are announced without a diagnostic cap; only 64 WAV spans are opened',()=>fixture(async f=>{
 const slots=Array.from({length:257},()=>f.remote());await f.tick();for(const slot of slots)slot.packets=10;
 await f.tick();await f.start('many-sources',{sourceOutcomes:true});
 const before=f.messages.filter(m=>m.type==='packet'&&m.packet.type==='sources').length;
 await f.scan();assert.equal(f.messages.filter(m=>m.type==='packet'&&m.packet.type==='sources').length,before,'unchanged refused sources do not create repeated durable writes');
 await f.stop();
 const sources=new Map();for(const message of f.messages.filter(m=>m.type==='packet'&&m.packet.type==='sources'))
  for(const source of message.packet.discoveredSources)sources.set(source.sourceTrackId,source);
 assert.equal(sources.size,257);assert.equal(f.captures.length,64);
 assert.equal([...sources.values()].filter(s=>s.failed).length,193);
 assert.ok(f.messages.filter(m=>m.type==='packet'&&m.packet.type==='sources').every(m=>m.packet.discoveredSources.length===1));
}));

test('native-first Stop refuses a late discovery without creating a retry or false source failure',()=>fixture(async f=>{
 const remote=f.remote();await f.tick();remote.packets=10;await f.tick();f.nativeStopFirst();
 await f.start('native-finished',{sourceOutcomes:true});await f.stop();
 const sources=f.messages.filter(m=>m.type==='packet'&&m.packet.type==='sources');
 assert.equal(sources.length,1);assert.equal(sources[0].packet.discoveredSources[0].failed,false);
 assert.equal(f.captures.length,0);
 assert.deepEqual(f.messages.filter(m=>m.type==='warning'||m.type==='error'),[]);
}));

test('retry ids before a successful track ACK cannot exhaust the WAV allocation budget',()=>fixture(async f=>{
 const remote=f.remote();await f.tick();remote.packets=10;await f.tick();await f.start('retry-before-track',{sourceOutcomes:true});
 const ids=Array.from({length:70},()=>f.captures[0].nextSpanId());
 assert.ok(ids.every(id=>typeof id==='string'&&id.length>0));assert.equal(new Set(ids).size,70);
 assert.equal(f.messages.filter(m=>m.type==='packet'&&m.packet.type==='track').length,1);
}));

for(const cloned of [false,true])test(`rendered receiver ${cloned?'clone':'alias'} never bypasses connected/RTP admission`,()=>fixture(async f=>{
 const slot=f.remote();f.render(cloned?slot.receiver.track.clone():slot.receiver.track);
 f.peer.connectionState='new';f.peer.iceConnectionState='new';await f.tick();await f.start('rendered',{sourceOutcomes:true});
 assert.equal(f.captures.length,0,'rendering an unconnected receiver is not a standalone media source');
 f.peer.connectionState='connected';f.peer.iceConnectionState='connected';await f.tick();await f.scan();
 assert.equal(f.captures.length,0,'zero-packet receiver aliases remain excluded');
 slot.packets=10;await f.tick();await f.scan();assert.equal(f.captures.length,1);assert.equal(f.captures[0].kind,'remote');
 const root=f.captures[0].sourceTrackId;f.peer.close();await f.scan();
 assert.equal(f.captures.length,1,'a closed receiver cannot return through its rendered alias');
 assert.equal(f.captures[0].sourceTrackId,root);assert.equal(f.captures[0].stopped,true);
}));

test('starting after an address change refreshes the call generation before the first scan',()=>fixture(async f=>{
 f.window.location.href='https://meet.test/call-B';
 f.peer.senders=[{track:{id:'new-call-mic',label:'Microphone',kind:'audio',readyState:'live',enabled:true,muted:false},getParameters:()=>({encodings:[]})}];
 // Bind the existing peer to B, but deliberately do not run discovery first.
 await f.peer.setRemoteDescription({type:'answer',sdp:'B'});
 await f.start('B',{sourceOutcomes:true});assert.equal(f.captures.length,1);
 assert.equal(f.messages.find(m=>m.type==='packet'&&m.packet.type==='track').token,'B');
}));

test('a replacement relay is ready only after the departing capture token drains',()=>fixture(async f=>{
 const remote=f.remote();await f.tick();remote.packets=10;await f.tick();await f.start('departing');
 await f.receive({type:'handshake',protocol:1,instanceId:'replacement'});
 assert.equal(f.messages.some(m=>m.type==='hookReady'),false);
 await f.stop();
 assert.equal(f.messages.find(m=>m.type==='hookReady').instanceId,'replacement');
 assert.equal(f.messages.find(m=>m.type==='hookReady').revision,2,'the relay accepts only a hook with the display-share exclusion');
 await f.start('replacement-capture');
 assert.equal(f.captures.length,2,'the same live peer can record through the replacement relay');
}));


test('duplicate stops during a flush cannot close the shared context or announce replacement readiness early',async()=>{
 let release,entered,closeCount=0,stopCount=0;
 const flushing=new Promise(resolve=>release=resolve),stopping=new Promise(resolve=>entered=resolve);
 class Context {state='running';async close(){assert.equal(this.state,'running');this.state='closed';closeCount++}}
 await fixture(async f=>{
  const remote=f.remote();await f.tick();remote.packets=10;await f.tick();await f.start('departing');
  await f.receive({type:'handshake',protocol:1,instanceId:'replacement'});
  const first=f.stop();
  try {
   await stopping;
   await f.receive({type:'stop',token:'departing'});
   assert.equal(stopCount,1,'one recorder stop owns the flush');
   assert.equal(closeCount,0,'context remains open through the flush');
   assert.equal(f.messages.some(m=>m.type==='stopped'||m.type==='hookReady'),false);
  } finally {release();await first}
  assert.equal(closeCount,1);
  assert.equal(f.messages.filter(m=>m.type==='stopped').length,1);
  assert.equal(f.messages.filter(m=>m.type==='hookReady').length,1);
  assert.ok(f.messages.findIndex(m=>m.type==='stopped')<f.messages.findIndex(m=>m.type==='hookReady'));
  await f.start('replacement');
  assert.equal(f.captures.length,2,'replacement starts only after the old flush');
 },{Context,capture:async options=>{
  options.getSharedContext();let ending=false;
  return {async stop(){stopCount++;if(ending)return;ending=true;entered();await flushing}};
 }});
});


test('a cached-constructor receiver observed through its getter is captured once after inbound progress',()=>fixture(async f=>{
 const pc=f.cachedPeer(),remote=f.remote('Remote source',pc);f.render(remote.receiver.track);
 pc.getReceivers();await f.tick();await f.start('getter-recovery');
 assert.equal(f.captures.length,0,'a live rendered receiver without inbound progress is not admitted');
 remote.packets=10;await f.tick();await f.scan();
 assert.equal(f.captures.length,1);assert.equal(f.captures[0].kind,'remote');
 await f.scan();assert.equal(f.captures.length,1,'DOM alias does not duplicate the receiver span');
}));


test('internal capture scans and sender checks use original getters',()=>fixture(async f=>{
 const remote=f.remote();await f.tick();remote.packets=10;await f.tick();
 const sender={track:{id:'mic',label:'Microphone',kind:'audio',readyState:'live',enabled:true,muted:false},getParameters:()=>({encodings:[]})};
 f.peer.senders=[sender];
 for(const name of ['getTransceivers','getSenders','getReceivers'])f.peer[name]=()=>{throw Error('library getter '+name+' must not be called internally');};
 await f.start();assert.equal(f.captures.length,2);
 assert.equal(f.captures.find(c=>c.kind==='local').enabled(),true);
 await f.scan();assert.equal(f.captures.length,2);
 f.peer.close();await f.scan();assert.ok(f.captures.every(c=>c.stopped));
}));

test('closed connection with stale connected ICE stops capture and cannot return through teardown',()=>fixture(async f=>{
 const remote=f.remote();await f.tick();remote.packets=10;await f.tick();await f.start();
 assert.equal(f.captures.length,1);f.render(remote.receiver.track);
 f.peer.connectionState='closed';await f.scan();
 assert.equal(f.captures[0].stopped,true);
 for(const name of ['getReceivers','getSenders','getTransceivers','getStats','close'])f.peer[name]();
 f.peer.connectionState='connected';f.peer.signalingState='stable';remote.packets=20;await f.tick();await f.scan();
 assert.equal(f.captures.length,1,'terminal peer cannot open a replacement span');
}));

test('display-share audio senders and previews are excluded while call microphone senders record as local',()=>fixture(async f=>{
 const [mic]=(await f.navigator.mediaDevices.getUserMedia({audio:true})).getAudioTracks();
 const [display]=(await f.navigator.mediaDevices.getDisplayMedia({video:true,audio:true})).getAudioTracks();
 const preExistingDisplay=new f.Track({id:'pre-display',label:'Shared screen',kind:'audio',readyState:'live',
  enabled:true,muted:false,getSettings:()=>({displaySurface:'monitor'})});
 f.render(display);
 f.render(preExistingDisplay);
 f.peer.senders=[
  {track:mic,getParameters:()=>({encodings:[]})},
  {track:display.clone(),getParameters:()=>({encodings:[]})},
  {track:preExistingDisplay,getParameters:()=>({encodings:[]})}
 ];
 await f.start('local-only',{captureLocal:true,sourceOutcomes:true});
 assert.equal(f.captures.length,1);
 assert.equal(f.captures[0].kind,'local');
 assert.equal(f.captures[0].label,'local: Headset Microphone');
}));

const micSender=(track,extra={})=>({track,getParameters:()=>({encodings:[]}),...extra});
test('display-share audio cloned through MediaStream.clone() is never recorded as Me or page media',()=>fixture(async f=>{
 const [mic]=(await f.navigator.mediaDevices.getUserMedia({audio:true})).getAudioTracks();
 const shared=await f.navigator.mediaDevices.getDisplayMedia({video:true,audio:true});
 const [streamClone]=shared.clone().getAudioTracks();
 assert.notEqual(streamClone,shared.getAudioTracks()[0]);
 f.render(streamClone);
 f.peer.senders=[micSender(mic),micSender(streamClone)];
 await f.start('local-only',{captureLocal:true,sourceOutcomes:true});
 assert.deepEqual(f.captures.map(c=>c.label),['local: Headset Microphone']);
}));

test('one microphone track sent on two peers stays audible while either sends, through peer close and null mute',()=>fixture(async f=>{
 const [mic]=(await f.navigator.mediaDevices.getUserMedia({audio:true})).getAudioTracks();
 const second=new f.window.RTCPeerConnection();
 const first=micSender(mic),later=micSender(mic);
 f.peer.senders=[first];second.senders=[later];
 await f.start('mesh',{captureLocal:true,sourceOutcomes:true});
 assert.equal(f.captures.length,1,'one Me source for one microphone');
 const me=f.captures[0];assert.equal(me.enabled(),true);
 later.track=null;await f.scan();
 assert.equal(me.enabled(),true,'the first peer still transmits the microphone');
 later.track=mic;await f.scan();
 second.close();await f.scan();
 assert.equal(f.captures.length,1);assert.equal(me.stopped,false);
 assert.equal(me.enabled(),true,'closing one peer keeps Me while the other still sends');
 first.track=null;await f.scan();
 assert.equal(me.enabled(),false,'muted on every remaining peer is silence');
 first.track=mic;await f.scan();
 assert.equal(me.enabled(),true);assert.equal(f.captures.length,1);
}));

test('microphone clones sent on several peers record one Me source, switching to a clone that still transmits',()=>fixture(async f=>{
 const [mic]=(await f.navigator.mediaDevices.getUserMedia({audio:true})).getAudioTracks();
 const second=new f.window.RTCPeerConnection();
 const a=micSender(mic.clone()),b=micSender(mic.clone());
 f.peer.senders=[a];second.senders=[b];
 await f.start('mesh',{captureLocal:true,sourceOutcomes:true});
 assert.equal(f.captures.length,1,'clones of one microphone are one voice');
 const sourceTrackId=f.captures[0].sourceTrackId;
 a.track.enabled=false;await f.scan();
 const live=f.captures.filter(c=>!c.stopped);
 assert.equal(live.length,1);assert.equal(live[0].sourceTrackId,sourceTrackId,'the same Me source continues');
 assert.equal(live[0].enabled(),true,'Me follows the clone the other peer still transmits');
 const announced=f.messages.filter(m=>m.type==='packet'&&m.packet.type==='sources').flatMap(m=>m.packet.discoveredSources);
 assert.deepEqual([...new Set(announced.filter(s=>s.kind==='local').map(s=>s.sourceTrackId))],[sourceTrackId]);
}));

test('a restarted capture keeps the same Me source identity with fresh spans',()=>fixture(async f=>{
 const [mic]=(await f.navigator.mediaDevices.getUserMedia({audio:true})).getAudioTracks();
 f.peer.senders=[micSender(mic)];
 await f.start('before-loss',{captureLocal:true,sourceOutcomes:true});
 const first=f.captures[0];await f.stop();
 await f.start('resumed',{captureLocal:true,sourceOutcomes:true});
 assert.equal(f.captures.length,2);
 assert.equal(f.captures[1].sourceTrackId,first.sourceTrackId,'resume continues the same Me source');
 assert.notEqual(f.captures[1].id,first.id);
}));

test('replacing a sender track with a clone of another sender microphone converges on one Me source',()=>fixture(async f=>{
 const [headset]=(await f.navigator.mediaDevices.getUserMedia({audio:true})).getAudioTracks();
 const [desk]=(await f.navigator.mediaDevices.getUserMedia({audio:true})).getAudioTracks();
 const second=new f.window.RTCPeerConnection();
 const a=micSender(headset),b=micSender(desk);
 f.peer.senders=[a];second.senders=[b];
 await f.start('mesh',{captureLocal:true,sourceOutcomes:true});
 assert.equal(f.captures.length,2,'two microphones are two sources');
 b.track=headset.clone();await f.scan();
 const live=f.captures.filter(c=>!c.stopped);
 assert.equal(live.length,1,'the same microphone is never recorded twice');
 assert.equal(live[0].enabled(),true);
}));

test('a sender that switches from a shared microphone to another one records the new microphone as its own Me source',()=>fixture(async f=>{
 const [headset]=(await f.navigator.mediaDevices.getUserMedia({audio:true})).getAudioTracks();
 const [desk]=(await f.navigator.mediaDevices.getUserMedia({audio:true})).getAudioTracks();
 const second=new f.window.RTCPeerConnection();
 const a=micSender(headset),b=micSender(headset.clone());
 f.peer.senders=[a];second.senders=[b];
 await f.start('mesh',{captureLocal:true,sourceOutcomes:true});
 assert.equal(f.captures.length,1,'one shared microphone is one Me source');
 const shared=f.captures[0];
 b.track=desk;await f.scan();
 const live=f.captures.filter(c=>!c.stopped);
 assert.equal(live.length,2,'the switched sender microphone is recorded too');
 const switched=live.find(c=>c!==shared);
 assert.ok(switched,'the shared microphone capture continues');
 assert.equal(shared.stopped,false);
 assert.notEqual(switched.sourceTrackId,shared.sourceTrackId,'two microphones are two Me sources');
 assert.equal(shared.enabled(),true);assert.equal(switched.enabled(),true);
 b.track=headset.clone();await f.scan();
 const after=f.captures.filter(c=>!c.stopped);
 assert.deepEqual(after.map(c=>c.sourceTrackId),[shared.sourceTrackId],'switching back converges on the shared Me source');
}));

test('a sender that switched microphones between captures is its own Me source after resume',()=>fixture(async f=>{
 const [headset]=(await f.navigator.mediaDevices.getUserMedia({audio:true})).getAudioTracks();
 const [desk]=(await f.navigator.mediaDevices.getUserMedia({audio:true})).getAudioTracks();
 const second=new f.window.RTCPeerConnection();
 const a=micSender(headset),b=micSender(headset);
 f.peer.senders=[a];second.senders=[b];
 await f.start('before-loss',{captureLocal:true,sourceOutcomes:true});
 const shared=f.captures[0].sourceTrackId;await f.stop();
 b.track=desk;
 await f.start('resumed',{captureLocal:true,sourceOutcomes:true});
 const live=f.captures.filter(c=>!c.stopped);
 assert.equal(live.length,2,'both microphones record after resume');
 assert.ok(live.some(c=>c.sourceTrackId===shared),'the shared microphone keeps its Me source');
}));

test('whichever sender switches away from a shared microphone, the shared one keeps its Me source',()=>fixture(async f=>{
 const [headset]=(await f.navigator.mediaDevices.getUserMedia({audio:true})).getAudioTracks();
 const [desk]=(await f.navigator.mediaDevices.getUserMedia({audio:true})).getAudioTracks();
 const second=new f.window.RTCPeerConnection();
 const a=micSender(headset.clone()),b=micSender(headset);
 f.peer.senders=[a];second.senders=[b];
 await f.start('mesh',{captureLocal:true,sourceOutcomes:true});
 const shared=f.captures[0].sourceTrackId;
 a.track=desk;await f.scan();
 const live=f.captures.filter(c=>!c.stopped);
 assert.equal(live.length,2,'both microphones record');
 assert.equal(new Set(live.map(c=>c.sourceTrackId)).size,2);
 const headsetCapture=live.find(c=>c.sourceTrackId===shared);
 assert.ok(headsetCapture,'the headset keeps the shared Me source');
 assert.equal(headsetCapture.enabled(),true);
}));
