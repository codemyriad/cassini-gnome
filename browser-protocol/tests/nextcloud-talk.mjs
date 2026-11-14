// Real Nextcloud Talk + signaling regression. The server is a disposable local
// harness, never a production call. Drive the real UI and shipped extension;
// do not fabricate meeting inventory, RTC peers, or page discovery messages.
import assert from 'node:assert/strict';
import {createServer} from 'node:net';
import {generateKeyPairSync,createHash} from 'node:crypto';
import {cp,mkdir,mkdtemp,readFile,writeFile,chmod,rm} from 'node:fs/promises';
import os from 'node:os';import path from 'node:path';import {fileURLToPath} from 'node:url';
import {joinNextcloudTalk,talkMediaState} from '../scenarios/nextcloud-talk.mjs';
import {installRtcTopologyProbe,summarizeRtcTopology,waitRtcTopology,summarizeRtcCohortComparison} from './rtc-topology-probe.mjs';
const {chromium}=await import(process.env.PLAYWRIGHT_MODULE||'playwright');
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const callURL=process.env.NEXTCLOUD_TEST_CALL_URL;
assert.ok(callURL,'NEXTCLOUD_TEST_CALL_URL must name a disposable local Talk room');
const room=new URL(callURL);
assert.ok(['127.0.0.1','localhost','[::1]'].includes(room.hostname),'Automated recording tests must not join public or production rooms');
const otherCallURL=process.env.NEXTCLOUD_TEST_OTHER_CALL_URL;
assert.ok(otherCallURL,'NEXTCLOUD_TEST_OTHER_CALL_URL must name another disposable room');
const otherRoom=new URL(otherCallURL);
assert.equal(otherRoom.origin,room.origin,'cohort comparison requires the same disposable backend');
assert.notEqual(otherRoom.pathname,room.pathname,'cohort comparison requires different rooms');
const temporary=await mkdtemp(path.join(os.tmpdir(),'cassini-nextcloud-'));
const extension=path.join(temporary,'extension'),profile=path.join(temporary,'profile');
const audioRoot=path.join(temporary,'recordings'),hosts=path.join(profile,'NativeMessagingHosts');
await cp(path.resolve(process.env.CASSINI_TEST_EXTENSION||path.join(root,'extension')),extension,{recursive:true});
await Promise.all([mkdir(audioRoot),mkdir(hosts,{recursive:true}),mkdir(path.join(root,'artifacts'),{recursive:true})]);
const manifestPath=path.join(extension,'manifest.json'),manifest=JSON.parse(await readFile(manifestPath,'utf8'));
const {publicKey}=generateKeyPairSync('rsa',{modulusLength:2048});const key=publicKey.export({type:'spki',format:'der'});
manifest.key=key.toString('base64');await writeFile(manifestPath,JSON.stringify(manifest));
const id=[...createHash('sha256').update(key).digest('hex').slice(0,32)].map(c=>String.fromCharCode(97+parseInt(c,16))).join('');
assert.notEqual(id,'pcafdaahfhjiolfpfnlhmljfgoabbaij');
const socket=path.join(temporary,'control.sock');let control;const events=[],pageErrors=[],controlListeners=new Set();
const server=createServer(peer=>{control=peer;let pending='';peer.setEncoding('utf8');peer.on('data',data=>{pending+=data;for(let end;(end=pending.indexOf('\n'))>=0;){events.push(JSON.parse(pending.slice(0,end)));pending=pending.slice(end+1);for(const notify of [...controlListeners])notify();}})});
await new Promise(resolve=>server.listen(socket,resolve));
for(const [name,fixture]of [['io.codemyriad.cassini','browser_host_fixture.py'],['io.codemyriad.cassini.control','control_host_fixture.py']]){
  const host=path.join(temporary,name+'.py');
  await writeFile(host,`#!/usr/bin/env python3\nimport os,sys\nos.environ['CASSINI_LIBRARY_DIR']=${JSON.stringify(audioRoot)}\nos.environ['CASSINI_CONTROL_SOCKET']=${JSON.stringify(socket)}\nsys.argv=[${JSON.stringify(path.join(root,'tests',fixture))},${JSON.stringify(audioRoot)}]\nexec(compile(open(sys.argv[0]).read(),sys.argv[0],'exec'))\n`);await chmod(host,0o700);
  await writeFile(path.join(hosts,name+'.json'),JSON.stringify({name,description:'Private Nextcloud test host',path:host,type:'stdio',allowed_origins:[`chrome-extension://${id}/`]}));
}
async function wave(frequency){
 const rate=48000,frames=rate*8,data=Buffer.alloc(44+frames*2);data.write('RIFF');data.writeUInt32LE(data.length-8,4);data.write('WAVEfmt ',8);data.writeUInt32LE(16,16);data.writeUInt16LE(1,20);data.writeUInt16LE(1,22);data.writeUInt32LE(rate,24);data.writeUInt32LE(rate*2,28);data.writeUInt16LE(2,32);data.writeUInt16LE(16,34);data.write('data',36);data.writeUInt32LE(frames*2,40);
 for(let i=0;i<frames;i++){const envelope=.3+.7*Math.sin(Math.PI*(i/rate%1))**2;data.writeInt16LE(Math.round(frequency?6000*envelope*Math.sin(2*Math.PI*frequency*i/rate):0),44+i*2);}
 const file=path.join(temporary,'input-'+frequency+'.wav');await writeFile(file,data);return file;
}
async function until(check,label,timeout=30000){const deadline=Date.now()+timeout;while(Date.now()<deadline){const value=await check();if(value)return value;await new Promise(resolve=>setTimeout(resolve,50));}throw Error('Timed out: '+label);}
// Native-control receipt waits are driven by socket messages, never a delay.
function controlEvent(check,label,timeout=30000){return new Promise((resolve,reject)=>{
 const finish=(value,error)=>{clearTimeout(timer);controlListeners.delete(changed);error?reject(error):resolve(value)};
 const changed=()=>{try{const value=check();if(value)finish(value)}catch(error){finish(null,error)}};
 const timer=setTimeout(()=>finish(null,Error('Timed out: '+label)),timeout);
 controlListeners.add(changed);changed();
});}
const inventory=()=>events.filter(e=>e.type==='meetings').at(-1)?.meetings||[];
const sharedArgs=['--autoplay-policy=no-user-gesture-required','--disable-audio-output','--use-fake-device-for-media-stream','--use-fake-ui-for-media-stream'];
const userAgent='Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36';
let observer,meeting,worker,popup;const participants=[];let success=false;
const report={extensionVersion:manifest.version,events:[],cases:[]};
try{
 observer=await chromium.launchPersistentContext(profile,{channel:'chromium',headless:true,userAgent,permissions:['microphone','camera'],args:[...sharedArgs,`--disable-extensions-except=${extension}`,`--load-extension=${extension}`,`--allowlisted-extension-id=${id}`,`--use-file-for-fake-audio-capture=${await wave(0)}`]});
 await observer.addInitScript(installRtcTopologyProbe);
 await observer.addInitScript(()=>{globalThis.cassiniTalkDiscovery=[];addEventListener('message',event=>{if(event.source===window&&event.data?.channel==='cassini-page'&&event.data.type==='discovery')cassiniTalkDiscovery.push(event.data)})});
 worker=observer.serviceWorkers()[0]||await observer.waitForEvent('serviceworker');
 await worker.evaluate(()=>{
  globalThis.nextcloudBrowserEvents=[];
  const record=value=>{nextcloudBrowserEvents.push({at:Date.now(),...value});if(nextcloudBrowserEvents.length>250)nextcloudBrowserEvents.shift();};
  chrome.tabs.onUpdated.addListener((tabId,change)=>record({type:'tabUpdated',tabId,change}));
  chrome.runtime.onConnect.addListener(port=>{
   if(port.name!=='cassini-frame')return;
   const metadata={tabId:port.sender?.tab?.id,frameId:port.sender?.frameId,documentId:port.sender?.documentId};
   record({type:'frameConnected',...metadata});
   port.onMessage.addListener(message=>{if(['discovery','pageState','relayHello','relayReady','relayUnavailable'].includes(message.type))record({type:'frameMessage',...metadata,message});});
   port.onDisconnect.addListener(()=>record({type:'frameDisconnected',...metadata}));
  });
 });
 meeting=await observer.newPage();meeting.on('pageerror',error=>pageErrors.push(error.message));
 await joinNextcloudTalk(meeting,{url:callURL,name:'Cassini receiver',muted:true});
 for(const [name,frequency]of [['Hypatia',440],['Ibn al-Haytham',880]]){
  const browser=await chromium.launch({channel:'chromium',headless:true,args:[...sharedArgs,`--use-file-for-fake-audio-capture=${await wave(frequency)}`]});participants.push(browser);
  const context=await browser.newContext({permissions:['microphone','camera'],userAgent});
  await joinNextcloudTalk(await context.newPage(),{url:callURL,name});
 }
 // This is independent evidence that Talk is receiving live remote media,
 // obtained before asserting any extension inventory or issuing a Start.
 await until(async()=>{
  const state=await talkMediaState(meeting);
  return state.remoteAudio>=2&&state;
 },'two real Talk remote audio sources');
 report.media=await talkMediaState(meeting);
 const row=await until(()=>inventory().find(m=>m.sourceCount>=2&&m.heard===true),'extension detects both real Talk peers with RTP evidence',20000);
 report.cases.push('real Nextcloud sources detected');
 // A real toolbar popup sees the meeting as Chrome's active tab. This test
 // opens its resources as an extension page, so route only that metadata query
 // to Chrome's actual meeting-tab object, as in the loaded-reload fixture.
 popup=await observer.newPage();await popup.goto(`chrome-extension://${id}/popup.html`);
 await popup.evaluate(tabId=>{const query=chrome.tabs.query.bind(chrome.tabs);chrome.tabs.query=async options=>options.active?[await chrome.tabs.get(tabId)]:query(options);},row.tabId);
 await popup.getByRole('heading',{name:'Interactive session is unverified',exact:true}).waitFor();
 const beforeStart=await popup.evaluate(tabId=>chrome.runtime.sendMessage({target:'background',type:'status',tabId}),row.tabId);
 assert.ok(inventory().some(item=>item.tabId===row.tabId&&item.sourceCount>=2&&item.heard===true),
  'listen-only Talk retains both RTP sources without automatic admission');
 assert.equal(beforeStart.tabEvidence.callEvidence.state,'unknown');
 const requestId='nextcloud-start';control.write(JSON.stringify({type:'command',requestId,action:'start',tabId:row.tabId,mode:'tracks',independent:true,captureLocal:false,microphone:false})+'\n');
 const started=await until(()=>events.find(e=>e.type==='result'&&e.requestId===requestId),'native Start result');assert.equal(started.ok,true,JSON.stringify(started));
 const directory=started.status.path;
 await until(async()=>{try{const m=JSON.parse(await readFile(path.join(directory,'session.json'),'utf8'));return m.tracks.filter(t=>t.kind==='remote'&&t.frames>=t.sampleRate*2).length>=2&&m;}catch{return null;}},'two separately captured remote tracks');
 await popup.getByRole('heading',{name:'Browser capture is active',exact:true}).waitFor();
 // This control client never opts in to popup-record-v1: Stop is shown for the
 // tab's capture but disabled with a reason, and the popup asks nothing.
 const stopButton=popup.getByRole('button',{name:'Stop recording',exact:true});await stopButton.waitFor();
 assert.equal(await stopButton.isDisabled(),true);
 assert.ok(!events.some(e=>e.type==='recordRequest'),'no popup ask without the app opt-in');
 const stopId='nextcloud-stop';control.write(JSON.stringify({type:'command',requestId:stopId,action:'stop',tabId:row.tabId,independent:true})+'\n');
 const stopped=await until(()=>events.find(e=>e.type==='result'&&e.requestId===stopId),'native Stop result');assert.equal(stopped.ok,true,JSON.stringify(stopped));
 const final=JSON.parse(await readFile(path.join(directory,'session.json'),'utf8'));report.manifest=final;
 assert.equal(final.state,'completed');assert.ok(!final.tracks.some(t=>t.kind==='local'||t.kind==='microphone'));
 const remoteTracks=final.tracks.filter(t=>t.kind==='remote');
 assert.equal(remoteTracks.length,2,'each of the two incoming peers must have its own track');
 assert.equal(new Set(remoteTracks.map(t=>t.sourceTrackId)).size,2,'incoming peers must have distinct source identities');
 const peaks=[];
 for(const track of remoteTracks){
  assert.ok(track.sourceTrackId);assert.equal(track.sampleRate,48000);assert.equal(track.channels,1);
  assert.equal(track.captureOutcome,'captured');assert.deepEqual(track.gaps,[]);assert.equal(track.droppedFrames,0);
  assert.equal(track.receivedFrames,track.frames);assert.ok(track.frames>=track.sampleRate*2);
  const pcm=await readFile(path.join(directory,track.file));
  assert.equal(pcm.toString('ascii',0,4),'RIFF');assert.equal(pcm.toString('ascii',8,12),'WAVE');
  assert.equal(pcm.readUInt16LE(20),1);assert.equal(pcm.readUInt16LE(22),1);
  assert.equal(pcm.readUInt32LE(24),48000);assert.equal(pcm.readUInt16LE(34),16);
  assert.equal(pcm.length-44,track.frames*2);
  const frames=Math.min(12000,Math.floor((pcm.length-44)/2));assert.ok(frames>=4000);
  function power(f){let re=0,im=0;for(let i=0;i<frames;i++){const sample=pcm.readInt16LE(pcm.length-frames*2+i*2)/32768;re+=sample*Math.cos(2*Math.PI*f*i/track.sampleRate);im+=sample*Math.sin(2*Math.PI*f*i/track.sampleRate);}return(re*re+im*im)/(frames*frames);}
  const a=power(440),b=power(880);
  assert.ok(Math.max(a,b)>1e-7&&Math.max(a,b)>Math.min(a,b)*10,'every remote track must contain one isolated test tone');
  const frequency=a>b?440:880,expectedName=frequency===440?'Hypatia':'Ibn al-Haytham';
  assert.equal(track.label,expectedName,'Talk participant name must match the isolated peer tone');
  assert.equal(track.labelSource,'dom','Talk participant labels must retain DOM provenance');
  const source=final.captureOutcome.sources.find(s=>s.sourceTrackId===track.sourceTrackId);
  assert.ok(source,'named audio track must have its own source ledger entry');
  assert.equal(source.label,expectedName);assert.equal(source.labelSource,'dom');
  peaks.push(frequency);
 }
 assert.deepEqual([...new Set(peaks)].sort((a,b)=>a-b),[440,880],'remote tracks must carry separate known peer tones without cross-mixing');
 // D80.3 / D-982: add a 3rd remote participant (4-person call: 3 remote + active mic)
 // and measure the real split-PC topology + sustained callSignals renewal.
 const thirdBrowser=await chromium.launch({channel:'chromium',headless:true,args:[...sharedArgs,`--use-file-for-fake-audio-capture=${await wave(660)}`]});participants.push(thirdBrowser);
 const thirdContext=await thirdBrowser.newContext({permissions:['microphone','camera'],userAgent});
 await joinNextcloudTalk(await thirdContext.newPage(),{url:callURL,name:'Al-Khwarizmi'});
 await until(async()=>{const state=await talkMediaState(meeting);return state.remoteAudio>=3&&state;},'three real Talk remote audio sources');
 report.rtcTopologyMuted=await meeting.evaluate(()=>cassiniRtcTopology());
 await meeting.getByRole('button',{name:/^Unmute(?: audio)?(?: \(.*\))?$/i}).first().click();
 const topologyBefore=await waitRtcTopology(meeting,{sending:'captured'});
 report.rtcTopology=summarizeRtcTopology(topologyBefore,await waitRtcTopology(meeting,{previous:topologyBefore,sending:'captured'}));
 assert.ok(report.rtcTopology.sendingPeers>0&&report.rtcTopology.receivingPeers>=3,'4-person Talk measurement requires real bidirectional packet progress with 3 remote peers');
 report.rtcCohortCandidates=await meeting.evaluate(()=>cassiniRtcCohortFacts());
 assert.equal(report.rtcTopology.samePeerConnection,false,'real Talk must exercise split publisher/subscriber PCs');
 // D80.5 product gate: the SHIPPED native collector and worker must publish
 // fresh active evidence for this same-document/frame cohort, not only a probe.
 const proofStart=events.length;
 control.write(JSON.stringify({type:'configure',capabilities:['call-signals-v1','verified-auto-admission-v1']})+'\n');
 const ack=await controlEvent(()=>events.slice(proofStart).find(event=>event.type==='configured'),'verified capability acknowledgement');
 assert.deepEqual([...ack.capabilities].sort(),['call-signals-v1','verified-auto-admission-v1'].sort());
 const qualifiedSignal=await controlEvent(()=>events.slice(proofStart).find(event=>event.type==='callSignals'&&event.tabs.some(tab=>tab.tabId===row.tabId&&tab.callEvidence.state==='active')),'real Talk verified active publication');
 const qualified=qualifiedSignal.tabs.find(tab=>tab.tabId===row.tabId);
 assert.equal(qualified.remoteAudio,'separated');assert.equal(qualified.callEvidence.adapter.version,'2');
 const verified=await controlEvent(()=>inventory().find(item=>item.tabId===row.tabId&&item.browserDocumentId===qualified.documentGeneration),'correlated verified RTP inventory');
 assert.equal(verified.documentGeneration,qualified.sourceDocumentGeneration);assert.equal(verified.callGeneration,qualified.callEvidence.callGeneration);
 assert.notEqual(qualified.documentGeneration,qualified.sourceDocumentGeneration);
 await popup.getByRole('heading',{name:'Interactive media session detected',exact:true}).waitFor();
 // Verify active callEvidence is renewed across multiple post-qualification sampling cycles without dropping to unknown.
 const renewalSignals=await controlEvent(()=>{
  const postActive=events.slice(proofStart).filter(event=>event.type==='callSignals'&&event.sequence>qualifiedSignal.sequence);
  return postActive.length>=3&&postActive;
 },'sustained active callSignals renewals');
 const renewals=renewalSignals.map(event=>event.tabs.find(tab=>tab.tabId===row.tabId));
 assert.ok(renewals.every(tab=>tab?.callEvidence.state==='active'&&tab.callEvidence.callGeneration===qualified.callEvidence.callGeneration&&tab.remoteAudio==='separated'),
  '4-person active Talk call must not drop to unknown across callSignals renewals');
 await popup.getByRole('heading',{name:'Interactive media session detected',exact:true}).waitFor();
 report.d805Admission={state:qualified.callEvidence.state,remoteAudio:qualified.remoteAudio,acknowledged:true,namespaceBinding:true,
   adapterVersion:qualified.callEvidence.adapter.version,splitPeerConnection:true,renewals:renewals.length,
   collector:await meeting.evaluate(()=>{const value=__cassiniCallEvidenceV1.read();return {version:value.version,
     cohortQualified:value.cohortQualified,samePeerCount:value.samePeerCount,receivedPeers:value.receivedPeers,capturedSendingPeers:value.capturedSendingPeers}})};
 assert.equal(report.d805Admission.collector.cohortQualified,true);assert.equal(report.d805Admission.collector.samePeerCount,0);
 assert.ok(report.d805Admission.collector.receivedPeers>=3,'4-person Talk call must report at least 3 receiving peers');
 assert.equal(report.d805Admission.collector.capturedSendingPeers,1);
 report.cases.push('D80.5 / D-982 real 4-person split-PC Talk qualifies and renews through native collector and worker');
 // Compare actual native fields against another room on the SAME backend.
 // A common SFU certificate/CNAME/stream placeholder is not room identity.
 // Retain only numeric facts plus a private in-browser reference table, then
 // leave A before joining B so Talk's one-call-per-session UI cannot interfere.
 await meeting.evaluate(()=>{
   const groups=new Map(),first=cassiniRtcCohortFacts(groups);
   window.cassiniCompareOtherRoom=()=>({first,second:window.cassiniCohortOther.cassiniRtcCohortFacts(groups)});
 });
 const leaveFirst=meeting.getByRole('button',{name:'Leave call',exact:true});
 await leaveFirst.click();await leaveFirst.waitFor({state:'hidden'});
 const otherPageEvent=meeting.waitForEvent('popup');
 await meeting.evaluate(()=>{window.cassiniCohortOther=window.open('about:blank')});
 const otherMeeting=await otherPageEvent;
 otherMeeting.on('pageerror',error=>pageErrors.push(error.message));
 await joinNextcloudTalk(otherMeeting,{url:otherCallURL,name:'Alice',muted:false});
 const otherContext=await participants[0].newContext({permissions:['microphone','camera'],userAgent});
 await joinNextcloudTalk(await otherContext.newPage(),{url:otherCallURL,name:'Bob'});
 const otherBefore=await waitRtcTopology(otherMeeting,{sending:'captured'});
 report.otherRoomRtcTopology=summarizeRtcTopology(otherBefore,await waitRtcTopology(otherMeeting,{previous:otherBefore,sending:'captured'}));
 assert.ok(report.otherRoomRtcTopology.sendingPeers>0&&report.otherRoomRtcTopology.receivingPeers>0,'other-room comparison requires actual captured sending and remote progress');
 const comparison=await meeting.evaluate(()=>cassiniCompareOtherRoom());
 for(const facts of [comparison.first,comparison.second])assert.ok(facts.peers.some(peer=>peer.remote.fingerprint.length>0),'both native room descriptions must be available for comparison');
 report.rtcCrossRoomCohortCandidates=summarizeRtcCohortComparison(comparison.first,comparison.second);
 report.cases.push('D80.3 same-backend/different-room native cohort comparison measured; no admission rule inferred');
 await otherContext.close();await otherMeeting.close();

 assert.deepEqual(pageErrors,[]);report.cases.push('DOM participant names match isolated remote PCM and persisted provenance, no microphone or tone cross-mixing');report.manifest=final;
 console.log('PASS real Nextcloud Talk: Hypatia=440Hz and Ibn al-Haytham=880Hz, distinct sources, DOM provenance, separate remote WAVs and finalization');success=true;
}finally{
 report.browserEvents=await worker?.evaluate(()=>nextcloudBrowserEvents).catch(error=>({error:error.message}));
 report.events=events;report.pageErrors=pageErrors;
 if(meeting&&!meeting.isClosed()){
  report.dialogs=await meeting.getByRole('dialog').evaluateAll(nodes=>nodes.map(d=>({text:d.innerText,buttons:[...d.querySelectorAll('button')].map(b=>({text:b.innerText,label:b.getAttribute('aria-label'),classes:b.className,disabled:b.disabled})),inputs:[...d.querySelectorAll('input')].map(i=>({label:i.getAttribute('aria-label'),placeholder:i.placeholder,value:i.value}))}))).catch(()=>[]);
  if(!success)await meeting.screenshot({path:path.join(root,'artifacts/nextcloud-failure.png')}).catch(()=>{});
 }
 if(meeting&&!meeting.isClosed())report.media=await talkMediaState(meeting).catch(error=>({error:error.message}));
 await writeFile(path.join(root,'artifacts/nextcloud-talk.json'),JSON.stringify(report,null,2));
 await Promise.allSettled(participants.map(browser=>browser.close()));await observer?.close();control?.destroy();await new Promise(resolve=>server.close(resolve));
 if(!success)await cp(audioRoot,path.join(root,'artifacts/nextcloud-recordings'),{recursive:true});
 await rm(temporary,{recursive:true,force:true});
}
