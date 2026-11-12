// Measure the proposed R1 predicate with real RTC/media and fake browser devices.
// D80.4/D80.5 define active as an interactive captured-media session in one
// document/frame, not answered intent: echo and split PCs are positives. Passive media stays
// negative. This is observer proof, not app admission/native recording proof.
import assert from 'node:assert/strict';
import {createServer} from 'node:http';
import {generateKeyPairSync,createHash} from 'node:crypto';
import {cp,readFile,mkdir,mkdtemp,rm,writeFile} from 'node:fs/promises';
import os from 'node:os';import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {installRtcTopologyProbe,summarizeRtcTopology,waitRtcTopology} from './rtc-topology-probe.mjs';
const {chromium}=await import(process.env.PLAYWRIGHT_MODULE||'playwright');
const directory=await mkdtemp(path.join(os.tmpdir(),'cassini-rtc-evidence-'));
const server=createServer((request,response)=>{response.setHeader('Content-Type','text/html');response.end('<!doctype html><title>Controlled RTC evidence study</title><main id="state"></main>');});
await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const extension=path.join(directory,'extension');
await cp(path.join(root,'extension'),extension,{recursive:true});
const manifestPath=path.join(extension,'manifest.json'),manifest=JSON.parse(await readFile(manifestPath,'utf8'));
const key=generateKeyPairSync('rsa',{modulusLength:2048}).publicKey.export({type:'spki',format:'der'});
manifest.key=key.toString('base64');await writeFile(manifestPath,JSON.stringify(manifest));
const extensionId=[...createHash('sha256').update(key).digest('hex').slice(0,32)].map(c=>String.fromCharCode(97+parseInt(c,16))).join('');
assert.notEqual(extensionId,'pcafdaahfhjiolfpfnlhmljfgoabbaij');
const pair=['call-signals-v1','verified-auto-admission-v1'];
import {privateControl,packet,barrier} from './rtc-evidence-control.mjs';
const report={decision:'D80.5',scope:'real loaded companion/worker/native-control fixture and R1 media; interactive captured-media session, not answered human intent; no GNOME/Mac adoption or native recording proof',completed:false,cases:[]};
async function gathered(page,index) {
  return page.evaluate(async index=>{
    const pc=peers[index];
    if(pc.iceGatheringState!=='complete')await new Promise(resolve=>{
      const changed=()=>{if(pc.iceGatheringState==='complete'){pc.removeEventListener('icegatheringstatechange',changed);resolve()}};
      pc.addEventListener('icegatheringstatechange',changed);changed();
    });
    return pc.localDescription.toJSON();
  },index);
}
try{
  for(const fixture of ['duplex','passive-viewer','mic-preview','synthetic-sender','captured-clone','unrelated-mic-peer','unrelated-mic-same-backend','echo','unanswered-early-media','unanswered-ringing-no-capture','cross-frame-capture','cross-frame-split','cross-document-split','forged-page-evidence','repaired-frame-positive-media','primordial-poison-positive'].filter(name=>!process.env.CASSINI_RTC_CASE||name===process.env.CASSINI_RTC_CASE)){
    report.runningFixture=fixture;
    console.log("RTC fixture:",fixture);
    const context=await chromium.launchPersistentContext(path.join(directory,'profile-'+fixture),{channel:'chromium',headless:true,
      permissions:['microphone','clipboard-read','clipboard-write'],args:['--disable-audio-output','--autoplay-policy=no-user-gesture-required',
      '--use-fake-device-for-media-stream','--use-fake-ui-for-media-stream','--allow-loopback-in-peer-connection','--disable-features=WebRtcHideLocalIpsWithMdns',
      `--disable-extensions-except=${extension}`,`--load-extension=${extension}`,`--allowlisted-extension-id=${extensionId}`]});
    report.browserVersion=context.browser()?.version()||'Chromium';
    try{
      await context.addInitScript(installRtcTopologyProbe);
      const worker=context.serviceWorkers()[0]||await context.waitForEvent('serviceworker');await privateControl(worker);
      await context.addInitScript(()=>{
        globalThis.cassiniObservedDiscovery=null;
        addEventListener('message',event=>{
          if(event.source===window&&event.data?.channel==='cassini-page'&&event.data.type==='discovery'){
            const value=event.data;
            cassiniObservedDiscovery={sourceCount:value.sourceCount,heard:value.heard,hookCallGeneration:value.callGeneration};
          }
        });
      });
      const caller=await context.newPage(),remote=await context.newPage();
      const url=`http://127.0.0.1:${server.address().port}/`;
      await Promise.all([caller.goto(url+'caller'),remote.goto(url+'remote')]);
      await caller.evaluate(async fixture=>{
        window.peers=[new RTCPeerConnection()];window.fixture=fixture;window.capture=null;
        document.querySelector('#state').textContent=fixture.startsWith('unanswered-')?'Ringing':'Controlled RTC';
        if(!['passive-viewer','synthetic-sender','unanswered-ringing-no-capture','forged-page-evidence','cross-frame-capture','cross-frame-split','cross-document-split'].includes(fixture))window.capture=await navigator.mediaDevices.getUserMedia({audio:true});
        if(fixture==='cross-frame-capture'){
          const iframe=document.createElement('iframe');iframe.src='/child';
          await new Promise(resolve=>{iframe.onload=resolve;document.body.append(iframe)});
          window.capture=await iframe.contentWindow.navigator.mediaDevices.getUserMedia({audio:true});
        }
        if(fixture.startsWith('unrelated-mic-'))peers.push(new RTCPeerConnection());
        if(fixture==='synthetic-sender'){
          window.audio=new AudioContext();const tone=audio.createOscillator(),output=audio.createMediaStreamDestination();tone.connect(output);tone.start();await audio.resume();
          peers[0].addTrack(output.stream.getAudioTracks()[0]);
        }else if(capture&&!['mic-preview','unrelated-mic-peer','unrelated-mic-same-backend'].includes(fixture)){
          const track=capture.getAudioTracks()[0];peers[0].addTrack(fixture==='captured-clone'?track.clone():track);
        }else if(fixture.startsWith('unrelated-mic-'))peers[1].addTrack(capture.getAudioTracks()[0]);
        peers[0].addTransceiver('audio',{direction:'recvonly'});
      },fixture);
      let separateFrame=null,separatePage=null;
      if(fixture==='cross-frame-split'){
        await caller.evaluate(()=>new Promise(resolve=>{const iframe=document.createElement('iframe');iframe.id='split-capture';iframe.src='/sender';iframe.onload=resolve;document.body.append(iframe)}));
        separateFrame=caller.frames().find(frame=>frame.url()===url+'sender');
      }else if(fixture==='cross-document-split'){
        const opened=caller.waitForEvent('popup');await caller.evaluate(()=>{window.splitCapture=open('/sender')});
        separatePage=await opened;await separatePage.waitForLoadState('domcontentloaded');separateFrame=separatePage;
      }
      if(separateFrame){
        await separateFrame.evaluate(async()=>{
          window.capture=await navigator.mediaDevices.getUserMedia({audio:true});window.peers=[new RTCPeerConnection()];
          peers[0].addTransceiver(capture.getAudioTracks()[0],{direction:'sendonly'});
        });
        await caller.evaluate(fixture=>peers.push((fixture==='cross-frame-split'?document.querySelector('#split-capture').contentWindow:splitCapture).peers[0]),fixture);
      }
      await remote.evaluate(()=>{window.peers=[];window.audio=new AudioContext()});
      const peerCount=fixture.startsWith('unrelated-mic-')||separateFrame?2:1;
      async function negotiate(){for(let index=0;index<peerCount;index++){
        await caller.evaluate(async index=>{await peers[index].setLocalDescription(await peers[index].createOffer())},index);
        const offer=await gathered(caller,index);
        await remote.evaluate(async({index,offer,fixture})=>{
          if(fixture==='unrelated-mic-same-backend')window.fixtureCertificate??=await RTCPeerConnection.generateCertificate({name:'ECDSA',namedCurve:'P-256'});
          const pc=new RTCPeerConnection(fixture==='unrelated-mic-same-backend'?{certificates:[window.fixtureCertificate]}:undefined);peers[index]=pc;
          await pc.setRemoteDescription(offer);
          if(index===0){
            let stream;
            if(fixture==='echo'){
              const receiver=pc.getReceivers().find(receiver=>receiver.track?.kind==='audio');
              if(receiver){
                const input=audio.createMediaStreamSource(new MediaStream([receiver.track]));
                const output=audio.createMediaStreamDestination();input.connect(output);stream=output.stream;
              }
            }
            if(!stream){const tone=audio.createOscillator(),output=audio.createMediaStreamDestination();tone.connect(output);tone.start();stream=output.stream;}
            // Reuse a negotiated receive slot when possible; otherwise add a
            // real sender. Echo returns the actual received capture track.
            const slot=pc.getTransceivers().find(value=>value.receiver.track.kind==='audio');
            await slot.sender.replaceTrack(stream.getAudioTracks()[0]);slot.direction='sendrecv';
            await audio.resume();
          }
          await pc.setLocalDescription(await pc.createAnswer());
        },{index,offer,fixture});
        await caller.evaluate(async({index,answer})=>peers[index].setRemoteDescription(answer),{index,answer:await gathered(remote,index)});
      }
      }
      await negotiate();
      if(fixture==='forged-page-evidence')await caller.evaluate(()=>{
        postMessage({channel:'cassini-page',type:'callEvidence',state:'active',samePeerCount:1,callGeneration:'forged'},'*');
        try{Object.defineProperty(globalThis,'__cassiniCallEvidenceV1',{value:{read:()=>({samePeerCount:1})}})}catch{}
      });
      const sending=separateFrame||['passive-viewer','mic-preview','unanswered-ringing-no-capture','forged-page-evidence'].includes(fixture)?'none':'any';
      const before=await waitRtcTopology(caller,{sending});
      const topology=summarizeRtcTopology(before,await waitRtcTopology(caller,{previous:before,sending}));
      await caller.waitForFunction(()=>globalThis.__cassiniCallEvidenceV1?.read().revision>=3);
      const observed=await caller.evaluate(()=>{
        const value=globalThis.__cassiniCallEvidenceV1.read();
        return {revision:value.revision,ageMs:value.ageMs,receivedPeers:value.receivedPeers,
          capturedSendingPeers:value.capturedSendingPeers,samePeerCount:value.samePeerCount,cohortQualified:value.cohortQualified};
      });
      const candidateR1=topology.sendingPeers>0&&topology.receivingPeers>0;
      if(fixture==='unrelated-mic-same-backend'){
        const sharedRemoteCertificate=await caller.evaluate(async()=>{
          const fingerprints=await Promise.all(peers.map(async pc=>{
            const stats=await pc.getStats();
            const transport=[...stats.values()].find(value=>value.type==='transport'&&value.dtlsState==='connected');
            return stats.get(transport?.remoteCertificateId)?.fingerprint;
          }));
          return fingerprints.length===2&&fingerprints.every(value=>typeof value==='string'&&value.length>0)&&new Set(fingerprints).size===1;
        });
        assert.equal(sharedRemoteCertificate,true,'the unrelated microphone and passive receiver really share a native remote DTLS identity');
      }
      // Preserve the actual observed values even when a required positive or
      // negative fails, so fixture/algorithm failures remain diagnosable.
      const legacySourceObservation=await caller.evaluate(()=>cassiniObservedDiscovery);
      const observation={fixture,candidateR1,topology,packagedHookObservation:observed,legacySourceObservation,
        ...(fixture==='unrelated-mic-same-backend'?{nativeRemoteIdentityShared:true}:{}),
        ...(['echo','unanswered-early-media','unrelated-mic-peer','unrelated-mic-same-backend'].includes(fixture)?{acceptedPositive:'D80.4/D80.5 interactive captured-media session in one document/frame'}: {})};
      report.cases.push(observation);
      if(['passive-viewer','mic-preview','synthetic-sender','unanswered-ringing-no-capture','cross-frame-capture','cross-frame-split','cross-document-split','forged-page-evidence'].includes(fixture))assert.equal(candidateR1,false,fixture);
      if(['duplex','captured-clone','echo','unanswered-early-media','repaired-frame-positive-media','primordial-poison-positive'].includes(fixture))assert.equal(candidateR1,true,fixture);
      if(fixture.startsWith('unrelated-mic-')){assert.equal(candidateR1,true,'D80.5 '+fixture);assert.equal(topology.samePeerConnection,false);assert.equal(observed.samePeerCount,0)}
      assert.equal(observed.cohortQualified,candidateR1,'native document/frame cohort '+fixture);
      if(separateFrame){
        const first=await waitRtcTopology(separateFrame,{sending:'captured',receiving:false});
        const second=await waitRtcTopology(separateFrame,{previous:first,sending:'captured',receiving:false});
        observation.separateSenderTopology=summarizeRtcTopology(first,second);
        await separateFrame.waitForFunction(()=>__cassiniCallEvidenceV1.read().capturedSendingPeers>0);
        const foreign=await separateFrame.evaluate(()=>{const value=__cassiniCallEvidenceV1.read();return {cohortQualified:value.cohortQualified,receivedPeers:value.receivedPeers,capturedSendingPeers:value.capturedSendingPeers}});
        observation.separateSenderObservation=foreign;
        assert.equal(foreign.capturedSendingPeers,1);assert.equal(foreign.receivedPeers,0);assert.equal(foreign.cohortQualified,false);
      }
      if(['passive-viewer','mic-preview','synthetic-sender','unanswered-ringing-no-capture','cross-frame-capture','cross-frame-split','cross-document-split','forged-page-evidence'].includes(fixture))assert.equal(observed.samePeerCount,0,'packaged hook '+fixture);
      if(['duplex','captured-clone','echo','unanswered-early-media','repaired-frame-positive-media','primordial-poison-positive'].includes(fixture))assert.equal(observed.samePeerCount,1,'packaged hook '+fixture);
      if(fixture==='passive-viewer')await caller.waitForFunction(()=>cassiniObservedDiscovery?.sourceCount===1&&cassiniObservedDiscovery.heard===true);
      observation.legacySourceObservation=await caller.evaluate(()=>cassiniObservedDiscovery);
      const tabId=await worker.evaluate(async url=>(await chrome.tabs.query({})).find(tab=>tab.url===url).id,url+'caller');
      const hello=(await packet(worker,{type:'hello'})).packet;
      for(const capability of pair)assert.ok(hello.capabilities.includes(capability));
      if(fixture==='repaired-frame-positive-media'){
        await worker.evaluate(async tabId=>{
          await chrome.scripting.executeScript({target:{tabId,frameIds:[0]},world:'ISOLATED',func:()=>{globalThis.__cassiniRelay.retire();globalThis.__cassiniRepairInjection=true;}});
          await chrome.scripting.executeScript({target:{tabId,frameIds:[0]},world:'ISOLATED',files:['relay.js']});
        },tabId);
      }
      const after=await barrier(worker);
      const expected=candidateR1&&fixture!=='repaired-frame-positive-media'?'active':'unknown';
      const published=await packet(worker,{tabId,state:expected,after}).catch(async error=>{
        observation.workerFailure=await worker.evaluate(async tabId=>({
          states:fixturePackets.filter(packet=>packet.type==='callSignals').slice(-5).map(packet=>packet.tabs.map(row=>({tabId:row.tabId,state:row.callEvidence.state}))),
          isolated:(await chrome.scripting.executeScript({target:{tabId,frameIds:[0]},world:'ISOLATED',func:()=>({repair:globalThis.__cassiniRepairInjection,valid:globalThis.__cassiniRelay?.valid(),trusted:globalThis.__cassiniRelay?.observerDocumentStart})}))[0]?.result
        }),tabId);throw error;
      });
      if(separatePage){
        const senderTab=await worker.evaluate(async url=>(await chrome.tabs.query({})).find(tab=>tab.url===url).id,url+'sender');
        await packet(worker,{tabId:senderTab,state:'unknown',after:await barrier(worker)});
      }
      const trusted=fixture!=='repaired-frame-positive-media';
      const hookDocument=await caller.evaluate(()=>__cassiniCallEvidenceV1.read().documentGeneration);
      assert.equal(published.row.sourceDocumentGeneration,trusted?hookDocument:null);
      assert.notEqual(published.row.documentGeneration,published.row.sourceDocumentGeneration);
      let inventory;
      if(trusted){
        inventory=await packet(worker,{type:'meetings',tabId,after});
        assert.equal(inventory.row.browserDocumentId,published.row.documentGeneration);
        assert.equal(inventory.row.documentGeneration,published.row.sourceDocumentGeneration);
      }else{
        const verified=(await packet(worker,{type:'meetings',after})).packet;
        assert.equal(verified.meetings.some(row=>row.tabId===tabId),false,'unattested sources cannot inflate verified inventory');
        const legacy=await worker.evaluate(()=>{const index=fixturePackets.length;fixtureControlPort.onMessage.emit({type:'configure',capabilities:['call-signals-v1']});return index});
        inventory=await packet(worker,{type:'meetings',tabId,sourceCount:1,after:legacy});
        assert.equal(inventory.row.documentGeneration,hookDocument,'legacy RTP facts remain truthful without claiming verified binding');
        await packet(worker,{tabId,state:'unknown',after:await barrier(worker)});
      }
      assert.equal(inventory.row.sourceCount,1);assert.equal(inventory.row.heard,true);
      if(expected==='active')assert.equal(inventory.row.callGeneration,published.row.callEvidence.callGeneration);
      else assert.equal(published.row.callEvidence.callGeneration,null);
      assert.notEqual(inventory.row.callGeneration,'0');
      observation.worker={acknowledged:true,state:published.row.callEvidence.state,namespaceBinding:trusted,legacySourceCount:inventory.row.sourceCount,legacyHeard:inventory.row.heard};
      if(fixture==='primordial-poison-positive'){
        await worker.evaluate(async tabId=>chrome.scripting.executeScript({target:{tabId,frameIds:[0]},world:'MAIN',func:()=>{
        Object.getOwnPropertyDescriptor=()=>({value:{read:()=>({samePeerCount:99})}});
        Reflect.apply=()=>{throw Error('page Reflect')};
        Array.prototype.filter=Array.prototype.map=Array.prototype.some=()=>{throw Error('page Array')};
        Array.prototype[Symbol.iterator]=()=>{throw Error('page iteration')};
        Math.max=()=>Infinity;Math.min=()=>0;
        Set=Map=function(){throw Error('page collection')};
        performance.now=()=>0;
        Object.defineProperty(String.prototype,Symbol.split,{value(){throw Error('page split')}});
        Object.defineProperty(Promise,Symbol.species,{get(){throw Error('page species')}});
        RTCStatsReport.prototype.forEach=()=>{throw Error('page report')};
        RTCRtpReceiver.prototype.getStats=async()=>{throw Error('page receiver')};
        Object.defineProperty(RTCRtpSender.prototype,'track',{get(){throw Error('page track')}});
        Object.defineProperty(MediaStreamTrack.prototype,'readyState',{get(){return 'ended'}});
        }}),tabId);
        // Require three distinct post-injection publications: one old, not-yet
        // published native sample cannot make a broken collector look healthy.
        for(let refresh=0;refresh<3;refresh++){
          const after=await worker.evaluate(()=>fixturePackets.length);
          const renewed=await packet(worker,{tabId,state:'active',after});
          assert.equal(renewed.row.callEvidence.callGeneration,published.row.callEvidence.callGeneration);
        }
        observation.worker.nativeProgressSurvivesPagePrimordialPoison=true;
      }
      if(fixture==='duplex'){
        const initial=published.row.callEvidence.callGeneration;
        const refreshed=await packet(worker,{tabId,state:'active',after:await worker.evaluate(()=>fixturePackets.length)});
        assert.ok(refreshed.packet.sequence>published.packet.sequence);assert.equal(refreshed.row.callEvidence.callGeneration,initial);
        await caller.evaluate(()=>{capture.getAudioTracks()[0].enabled=false});
        await packet(worker,{tabId,state:'unknown',after:await barrier(worker)});
        await caller.evaluate(()=>{capture.getAudioTracks()[0].enabled=true});
        const unmuted=await packet(worker,{tabId,state:'active',after:await barrier(worker)});
        assert.equal(unmuted.row.callEvidence.callGeneration,initial,'mute/unmute retains identity');
        await caller.evaluate(()=>{capture.getAudioTracks().forEach(track=>track.stop());peers.forEach(pc=>pc.close())});
        const ended=await packet(worker,{tabId,state:'inactive',after:await barrier(worker)});
        assert.equal(ended.row.callEvidence.reason,'callEnded');assert.equal(ended.row.callEvidence.callGeneration,initial);
        await caller.evaluate(async()=>{
          window.capture=await navigator.mediaDevices.getUserMedia({audio:true});window.peers=[new RTCPeerConnection()];
          peers[0].addTrack(capture.getAudioTracks()[0]);peers[0].addTransceiver('audio',{direction:'recvonly'});
        });
        await remote.evaluate(()=>{peers.forEach(pc=>pc.close());window.peers=[]});
        await negotiate();
        await caller.waitForFunction(()=>Number(__cassiniCallEvidenceV1.read().hookGeneration)>=1&&__cassiniCallEvidenceV1.read().samePeerCount===1);
        const next=await packet(worker,{tabId,state:'active',after:await barrier(worker)});
        assert.notEqual(next.row.callEvidence.callGeneration,initial,'new interactive episode gets a background ID');
        assert.equal(next.row.documentGeneration,published.row.documentGeneration);assert.equal(next.row.sourceDocumentGeneration,published.row.sourceDocumentGeneration);
        observation.worker.terminalEndAndNewEpisode=true;
        const beforePing=await worker.evaluate(()=>{
          globalThis.fixtureProofControl=fixtureControlPort;
          const index=fixturePackets.length;fixtureControlPort.onMessage.emit({type:'ping',seq:701});return index;
        });
        assert.equal((await packet(worker,{type:'pong',after:beforePing})).packet.seq,701);
        const freezeIndex=await worker.evaluate(()=>fixturePackets.length),stoppedAt=Date.now();
        await caller.evaluate(()=>{const last=setInterval(()=>{},60000);for(let id=1;id<=last;id++)clearInterval(id)});
        await packet(worker,{tabId,state:'unknown',after:freezeIndex});
        // Policy deadlines are checked by the virtual publisher-timer unit test;
        // this Chromium check proves real hook withdrawal and the same healthy port.
        const withdrawalMs=Date.now()-stoppedAt;
        const afterPing=await worker.evaluate(()=>{
          if(fixtureControlPort!==fixtureProofControl)throw Error('Control connection changed during collector freeze');
          const index=fixturePackets.length;fixtureControlPort.onMessage.emit({type:'ping',seq:702});return index;
        });
        assert.equal((await packet(worker,{type:'pong',after:afterPing})).packet.seq,702);
        observation.worker.withdrawalWithHealthyPongMs=withdrawalMs;
      }
      if(fixture==='synthetic-sender'){
        // Exercise the real MAIN capture scan: DOM sender dedup must never
        // promote a generated sender into the collector's private gUM roots.
        await worker.evaluate(()=>{
          const previous=chrome.runtime.connectNative.bind(chrome.runtime);
          globalThis.fixtureManualStarts=0;globalThis.fixtureManualStops=0;
          chrome.runtime.connectNative=name=>{
            if(name!=='io.codemyriad.cassini')return previous(name);
            const event=()=>{const listeners=[];return {addListener:fn=>listeners.push(fn),emit:value=>listeners.forEach(fn=>fn(value))}};
            return {onMessage:event(),onDisconnect:event(),postMessage(value){
              if(value.type==='start')fixtureManualStarts++;if(value.type==='stop')fixtureManualStops++;
              queueMicrotask(()=>this.onMessage.emit(value.type==='start'?{type:'started',path:'/fixture/manual-synthetic',features:['source-outcomes-v1']}:
                value.type==='stop'?{type:'stopped',path:'/fixture/manual-synthetic'}:{type:'ack'}));
            },disconnect(){}};
          };
        });
        let index=await worker.evaluate(tabId=>{
          const index=fixturePackets.length;
          fixtureControlPort.onMessage.emit({type:'command',requestId:'synthetic-manual-start',action:'start',tabId,mode:'tracks',independent:true,captureLocal:false,microphone:false,keepOnNavigation:true});
          return index;
        },tabId);
        const start=(await packet(worker,{type:'result',after:index})).packet;
        assert.equal(start.requestId,'synthetic-manual-start');assert.equal(start.ok,true,JSON.stringify(start));
        await caller.evaluate(()=>{
          dispatchEvent(new PageTransitionEvent('pageshow',{persisted:true}));
          dispatchEvent(new PageTransitionEvent('pagehide',{persisted:true}));
          const doc=__cassiniCallEvidenceV1.read().documentGeneration;
          postMessage({channel:'cassini-page',type:'discovery',sourceCount:0,heard:false,documentGeneration:doc,callGeneration:'0'},'*');
          postMessage({channel:'cassini-page',type:'discovery',sourceCount:64,heard:true,documentGeneration:doc,callGeneration:'FORGED'},'*');
        });
        const revision=await caller.evaluate(()=>__cassiniCallEvidenceV1.read().revision);
        await caller.waitForFunction(revision=>__cassiniCallEvidenceV1.read().revision>=revision+3,revision);
        // Reconfiguration publishes a fresh protocol status snapshot; status
        // is a notification, not a host command action.
        const live=(await packet(worker,{type:'status',after:await barrier(worker)})).packet;
        assert.equal(live.sessions.some(session=>session.tabId===tabId&&session.recording===true),true,JSON.stringify(live));
        assert.equal(await worker.evaluate(()=>fixtureManualStops),0,'synthetic lifecycle cannot terminate the active recording');
        assert.equal(await caller.evaluate(()=>__cassiniCallEvidenceV1.read().samePeerCount),0);
        await packet(worker,{tabId,state:'unknown',after:await barrier(worker)});
        index=await worker.evaluate(tabId=>{
          const index=fixturePackets.length;
          fixtureControlPort.onMessage.emit({type:'command',requestId:'synthetic-manual-stop',action:'stop',tabId,independent:true});
          return index;
        },tabId);
        const stop=(await packet(worker,{type:'result',after:index})).packet;
        assert.equal(stop.requestId,'synthetic-manual-stop');assert.equal(stop.ok,true);
        await packet(worker,{tabId,state:'unknown',after:await barrier(worker)});
        assert.equal(await worker.evaluate(()=>fixtureManualStarts),1);
        observation.worker.manualCaptureDoesNotGrantProvenance=true;
      }
      const attempts=await worker.evaluate(()=>({inputs:fixtureCaptureAttempts,pcm:fixturePackets.some(value=>value.type==='audio'||value.type==='start')}));
      assert.equal(attempts.inputs,0);assert.equal(attempts.pcm,false);
      // The real popup RPC and Copy diagnostics use one content-free projection.
      const popup=await context.newPage();await popup.goto(`chrome-extension://${extensionId}/popup.html`);
      await popup.evaluate(tabId=>{const query=chrome.tabs.query.bind(chrome.tabs);chrome.tabs.query=async options=>options.active?[await chrome.tabs.get(tabId)]:query(options)},tabId);
      await popup.waitForFunction(()=>document.querySelector('main')?.getAttribute('aria-busy')==='false');
      await popup.waitForFunction(expected=>{
        const text=document.querySelector('#diagnostics').textContent;
        return text&&JSON.parse(text).callEvidence?.state===expected&&document.querySelector('#status').textContent===
          (expected==='active'?'Interactive media session detected':'Interactive session is unverified');
      },fixture==='duplex'?'unknown':expected);
      await popup.evaluate(()=>Object.defineProperty(navigator,'clipboard',{value:{writeText:async text=>{globalThis.fixtureCopy=text}}}));
      await popup.locator('summary').click();await popup.locator('#copy').click();await popup.waitForFunction(()=>typeof fixtureCopy==='string');
      assert.equal(await popup.evaluate(()=>fixtureCopy),await popup.locator('#diagnostics').textContent());
      const diagnostics=JSON.parse(await popup.locator('#diagnostics').textContent());
      assert.doesNotMatch(JSON.stringify(diagnostics),/127\.0\.0\.1|browserInstance|Generation|tabId|path|title|SDP/);
      observation.worker.diagnosticsContentFree=true;

    }finally{await context.close()}
  }
  report.completed=true;delete report.runningFixture;
}finally{
  await new Promise(resolve=>server.close(resolve));
  const artifacts=path.resolve('artifacts');await mkdir(artifacts,{recursive:true});
  await writeFile(path.join(artifacts,'rtc-evidence-observations.json'),JSON.stringify(report,null,2));
  await rm(directory,{recursive:true,force:true});
}
console.log('PASS loaded worker/native-control D80.5: same-document/frame captured-media cohorts qualify, including split PCs; passive, preview, synthetic, cross-frame and cross-document media remain unknown.');
