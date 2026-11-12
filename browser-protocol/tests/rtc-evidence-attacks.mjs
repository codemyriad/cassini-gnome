// Loaded-extension regressions, including a genuinely insecure origin. All RTC
// audio is generated; no gUM, native input, PCM sink, or installed profile.
import assert from 'node:assert/strict';
import {createServer} from 'node:http';
import {generateKeyPairSync,createHash} from 'node:crypto';
import {cp,mkdtemp,readFile,writeFile,rm} from 'node:fs/promises';
import os from 'node:os';import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {privateControl,packet,barrier} from './rtc-evidence-control.mjs';
import {connectReceiveSlots} from '../scenarios/product-topologies.mjs';
const {chromium}=await import(process.env.PLAYWRIGHT_MODULE||'playwright');
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const temporary=await mkdtemp(path.join(os.tmpdir(),'cassini-admission-attacks-'));
const server=createServer((request,response)=>{
 const attack=request.url.includes('/pre-handshake-forgery/')?`<script>
 const doc=__cassiniCallEvidenceV1.read()?.documentGeneration;
 globalThis.fixtureInlineAttacksSent=!!doc;
 if(doc){postMessage({channel:'cassini-page',type:'discovery',sourceCount:0,heard:false,documentGeneration:doc,callGeneration:'0'},'*');
 postMessage({channel:'cassini-page',type:'discovery',sourceCount:64,heard:true,documentGeneration:doc,callGeneration:'FORGED'},'*')}
 </script>`:'';
 response.end('<!doctype html><title>Permission-free adversary fixture</title>'+attack);
});
await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
try{
 for(const fixture of ['secure-loopback-rtp','insecure-origin-rtp','missing-peer','pre-handshake-forgery']){
  const extension=path.join(temporary,fixture+'-extension');await cp(path.join(root,'extension'),extension,{recursive:true});
  const manifestPath=path.join(extension,'manifest.json'),manifest=JSON.parse(await readFile(manifestPath,'utf8'));
  const key=generateKeyPairSync('rsa',{modulusLength:2048}).publicKey.export({type:'spki',format:'der'});
  manifest.key=key.toString('base64');manifest.content_scripts[0].js.unshift('fixture-capabilities.js');
  await writeFile(path.join(extension,'fixture-capabilities.js'),`addEventListener('message',event=>{if(event.data?.channel==='cassini-control'&&event.data.type==='handshake')globalThis.fixtureHandshake=event.data.instanceId});\n${fixture==='missing-peer'?'globalThis.RTCPeerConnection=undefined;':''}`);
  await writeFile(manifestPath,JSON.stringify(manifest));
  const id=[...createHash('sha256').update(key).digest('hex').slice(0,32)].map(c=>String.fromCharCode(97+parseInt(c,16))).join('');
  assert.notEqual(id,'pcafdaahfhjiolfpfnlhmljfgoabbaij');
  const context=await chromium.launchPersistentContext(path.join(temporary,fixture+'-profile'),{channel:'chromium',headless:true,
   permissions:[],args:['--use-fake-device-for-media-stream','--disable-audio-output','--no-proxy-server','--host-resolver-rules=MAP insecure.cassini.test 127.0.0.1',
    '--autoplay-policy=no-user-gesture-required','--allow-loopback-in-peer-connection','--disable-features=WebRtcHideLocalIpsWithMdns',
    `--disable-extensions-except=${extension}`,`--load-extension=${extension}`,`--allowlisted-extension-id=${id}`]});
  try{
   const worker=context.serviceWorkers()[0]||await context.waitForEvent('serviceworker');await privateControl(worker);
   const page=await context.newPage(),hostname=fixture==='insecure-origin-rtp'?'insecure.cassini.test':'127.0.0.1';
   const url=`http://${hostname}:${server.address().port}/${fixture}/`;
   await page.goto(url);
   const environment=await page.evaluate(()=>{
    const descriptor=Object.getOwnPropertyDescriptor(globalThis,'__cassiniCallEvidenceV1');
    return {secure:isSecureContext,uuid:typeof crypto.randomUUID,claimed:!!descriptor,
     frozen:Object.isFrozen(descriptor?.value),writable:descriptor?.writable,configurable:descriptor?.configurable,
     snapshot:descriptor?.value.read()};
   });
   assert.equal(environment.secure,fixture!=='insecure-origin-rtp');
   assert.equal(environment.claimed,true);assert.equal(environment.frozen,true);
   assert.equal(environment.writable,false);assert.equal(environment.configurable,false);
   if(fixture==='insecure-origin-rtp')assert.equal(environment.uuid,'undefined','this must exercise an actual insecure context');
   if(['insecure-origin-rtp','missing-peer'].includes(fixture))assert.equal(environment.snapshot,null);
   const permission=await page.evaluate(async()=>{try{return (await navigator.permissions.query({name:'microphone'})).state}catch{return 'unsupported'}});
   assert.notEqual(permission,'granted');
   const tabId=await worker.evaluate(async url=>(await chrome.tabs.query({})).find(tab=>tab.url===url).id,url);
   if(fixture==='missing-peer'){
    await page.waitForFunction(()=>typeof fixtureHandshake==='string');
    assert.equal(await page.evaluate(()=>{
     const message=new MessageEvent('message',{source:window,data:{channel:'cassini-page',type:'hookReady',protocol:1,revision:2,instanceId:fixtureHandshake}});
     dispatchEvent(message);return message.isTrusted;
    }),false);
    const synthetic=await worker.evaluate(tabId=>chrome.tabs.sendMessage(tabId,{type:'cassini-connect'}),tabId);
    assert.equal(synthetic.connected,false,'synthetic MessageEvent cannot complete the handshake');
    // Real postMessage is UA-trusted but page-forgeable; readiness alone must
    // still be harmless because the immutable unavailable reader was claimed.
    await page.evaluate(()=>postMessage({channel:'cassini-page',type:'hookReady',protocol:1,revision:2,instanceId:fixtureHandshake},'*'));
   }else{
    await page.evaluate(()=>{globalThis.fixtureDiscovery=null;addEventListener('message',event=>{if(event.data?.channel==='cassini-page'&&event.data.type==='discovery')fixtureDiscovery=event.data})});
    const remote=await context.newPage();await remote.goto(url+'remote');
    await connectReceiveSlots(page,remote,[[440]]);
    await page.waitForFunction(()=>fixtureDiscovery?.sourceCount===1&&fixtureDiscovery.heard===true);
    if(fixture==='insecure-origin-rtp'){
     const before=await worker.evaluate(()=>{const before=fixturePackets.length;fixtureControlPort.onMessage.emit({type:'configure',capabilities:['call-signals-v1']});return before});
     await packet(worker,{type:'meetings',tabId,sourceCount:1,after:before});
     const paired=await barrier(worker),verified=(await packet(worker,{type:'meetings',after:paired})).packet;
     assert.equal(verified.meetings.some(row=>row.tabId===tabId),false);
    }
    if(fixture==='secure-loopback-rtp'){
     await page.waitForFunction(()=>__cassiniCallEvidenceV1.read().heard===true);
     const navigation=page.waitForEvent('framenavigated',{predicate:frame=>frame.url()===url+'child'});
     await page.evaluate(url=>{const child=document.createElement('iframe');child.src=url+'child';document.body.append(child)},url);
     const child=await navigation;
     await child.evaluate(()=>{
      const forge=()=>postMessage({channel:'cassini-page',type:'discovery',sourceCount:64,heard:true,documentGeneration:'child-forged',callGeneration:'child-forged'},'*');
      addEventListener('message',event=>{if(event.data?.channel==='cassini-control'&&event.data.type==='discover')queueMicrotask(forge)});
      forge();
     });
     const before=await worker.evaluate(()=>{const before=fixturePackets.length;fixtureControlPort.onMessage.emit({type:'configure',capabilities:['call-signals-v1']});return before});
     await packet(worker,{type:'meetings',tabId,sourceCount:1,after:before});
     const paired=await barrier(worker);
     await packet(worker,{type:'meetings',tabId,sourceCount:1,after:paired});
    }
   }
   await page.evaluate(()=>{
    globalThis.forgedDescriptorReads=0;globalThis.forgedRevision=100;
    const forged=()=>({version:2,cohortQualified:true,documentGeneration:'X',hookGeneration:'Y',revision:++forgedRevision,ageMs:0,
     receivedPeers:1,capturedSendingPeers:1,samePeerCount:1,receivedVersion:forgedRevision,sentVersion:forgedRevision,
     ended:false,sessionEpoch:999,sourceCount:1,heard:true});
    try{Object.defineProperty(globalThis,'__cassiniCallEvidenceV1',{value:{read:forged}})}catch{}
    Object.getOwnPropertyDescriptor=()=>{forgedDescriptorReads++;return {value:{read:forged}}};
    try{Object.defineProperty(globalThis,'isSecureContext',{value:true})}catch{}
    const message=new MessageEvent('message',{source:window,data:{channel:'cassini-page',type:'discovery',sourceCount:64,heard:true,documentGeneration:'X',callGeneration:'Y'}});
    dispatchEvent(message);
    postMessage({channel:'cassini-page',type:'discovery',sourceCount:1,heard:true,documentGeneration:'X',callGeneration:'Y'},'*');
   });
   for(let attempt=0;attempt<3;attempt++){
    const after=await barrier(worker),current=await packet(worker,{tabId,after,state:'unknown'});
    assert.equal(current.row.callEvidence.callGeneration,null);assert.notEqual(current.row.sourceDocumentGeneration,'X');
   }
   assert.equal(await page.evaluate(()=>forgedDescriptorReads),0);
   if(fixture==='secure-loopback-rtp'){
    await page.evaluate(()=>{
     Object.prototype.then=function(resolve){resolve({samePeerCount:1,sourceCount:1,documentGeneration:'X'})};
     Object.prototype.toJSON=function(){return {samePeerCount:1,sourceCount:1,documentGeneration:'X'}};
    });
    await packet(worker,{tabId,state:'unknown',after:await barrier(worker)});
    assert.equal(await page.evaluate(()=>{const value=__cassiniCallEvidenceV1.read();return value.samePeerCount+':'+value.sourceCount}),'0:1');
   }else if(['insecure-origin-rtp','missing-peer'].includes(fixture))assert.equal(await page.evaluate(()=>__cassiniCallEvidenceV1.read()),null);
   assert.equal(await worker.evaluate(()=>fixtureCaptureAttempts),0);
   assert.equal(await worker.evaluate(()=>fixturePackets.some(packet=>packet.type==='callSignals'&&packet.tabs.some(row=>row.callEvidence.state==='active'))),false);
   if(fixture==='pre-handshake-forgery'){
    assert.equal(await page.evaluate(()=>fixtureInlineAttacksSent),true);
    await page.evaluate(()=>{
     dispatchEvent(new PageTransitionEvent('pageshow',{persisted:true}));
     dispatchEvent(new PageTransitionEvent('pagehide',{persisted:true}));
     const doc=__cassiniCallEvidenceV1.read().documentGeneration;
     postMessage({channel:'cassini-page',type:'discovery',sourceCount:0,heard:false,documentGeneration:doc,callGeneration:'0'},'*');
     postMessage({channel:'cassini-page',type:'discovery',sourceCount:64,heard:true,documentGeneration:doc,callGeneration:'FORGED'},'*');
    });
    await context.grantPermissions(['microphone'],{origin:new URL(url).origin});
    const offer=await page.evaluate(async()=>{
     const stream=await navigator.mediaDevices.getUserMedia({audio:true});
     const transceiver=topologyPeer.getTransceivers()[0];await transceiver.sender.replaceTrack(stream.getAudioTracks()[0]);
     transceiver.direction='sendrecv';await topologyPeer.setLocalDescription(await topologyPeer.createOffer());
     return topologyPeer.localDescription.toJSON();
    });
    const remote=context.pages().find(value=>value.url()===url+'remote');
    const answer=await remote.evaluate(async offer=>{
     const peer=topologyRemote.peer;await peer.setRemoteDescription(offer);peer.getTransceivers()[0].direction='sendrecv';
     await peer.setLocalDescription(await peer.createAnswer());return peer.localDescription.toJSON();
    },offer);
    await page.evaluate(answer=>topologyPeer.setRemoteDescription(answer),answer);
    await packet(worker,{tabId,state:'active',after:await barrier(worker)});
    assert.equal(await worker.evaluate(()=>fixtureCaptureAttempts),0);
   }
   console.log('PASS admission attack:',fixture,'— forgery refused, no native input'+(fixture==='pre-handshake-forgery'?'; later genuine captured R1 qualifies':''));
  }finally{await context.close()}
 }
}finally{await new Promise(resolve=>server.close(resolve));await rm(temporary,{recursive:true,force:true})}
