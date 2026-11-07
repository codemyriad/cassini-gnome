// Real BFCache navigation with the shipped hook/relay/worker. No fake lifecycle
// events, RTC objects or discovery messages. The retained case uses private
// native hosts and generated peers; no installed host or real device is used.
import assert from 'node:assert/strict';
import {createHash,generateKeyPairSync} from 'node:crypto';
import {createServer} from 'node:http';
import {createServer as createSocketServer} from 'node:net';
import {connectReceiveSlots} from '../scenarios/product-topologies.mjs';
import {cp,mkdir,mkdtemp,readFile,rm,writeFile,chmod} from 'node:fs/promises';
import os from 'node:os';import path from 'node:path';import {fileURLToPath} from 'node:url';
const {chromium}=await import(process.env.PLAYWRIGHT_MODULE||'playwright');
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
// Linux supports native-host manifests scoped to this private Chromium profile.
// The idle real-BFCache case is portable; neither case changes installed hosts.
const modes=process.env.BFCACHE_CASE?[process.env.BFCACHE_CASE]:process.platform==='linux'?['idle','retained']:['idle'];
for(const mode of modes) {
assert.ok(['idle','retained'].includes(mode));
const retained=mode==='retained';
assert.ok(!retained||process.platform==='linux','retained native BFCache proof needs private Linux profile manifests');
const temporary=await mkdtemp(path.join(os.tmpdir(),'cassini-bfcache-'));
const extension=path.join(temporary,'extension');
await cp(path.resolve(process.env.CASSINI_TEST_EXTENSION||path.join(root,'extension')),extension,{recursive:true});
const manifestPath=path.join(extension,'manifest.json'),manifest=JSON.parse(await readFile(manifestPath,'utf8'));
const {publicKey}=generateKeyPairSync('rsa',{modulusLength:2048}),key=publicKey.export({type:'spki',format:'der'});
manifest.key=key.toString('base64');await writeFile(manifestPath,JSON.stringify(manifest));
const id=[...createHash('sha256').update(key).digest('hex').slice(0,32)].map(c=>String.fromCharCode(97+parseInt(c,16))).join('');
assert.notEqual(id,'pcafdaahfhjiolfpfnlhmljfgoabbaij');
const server=createServer((request,response)=>{
 response.setHeader('Content-Type','text/html');
 // A natural COOP policy transition makes Chrome swap browsing instances even
 // on the same normalized call URL; no cache eligibility reason is bypassed.
 if(retained)response.setHeader('Cross-Origin-Opener-Policy',request.method==='POST'?'same-origin':'unsafe-none');
 response.end(`<!doctype html><title>BFCache fixture ${request.url}</title>
 <script>window.fixtureDocument=crypto.randomUUID();window.fixtureConstructor=RTCPeerConnection;window.fixtureLifecycle=[];
 for(const type of ['pageshow','pagehide'])addEventListener(type,e=>fixtureLifecycle.push({type,persisted:e.persisted}));</script>
 ${retained?'<form method="POST" action="/meeting#away"><button>Navigate away</button></form>':'<a href="/away">Navigate away</a>'}`);
});
await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
async function observed(check,label){const deadline=Date.now()+15000;while(true){const value=await check();if(value)return value;assert.ok(Date.now()<deadline,'Timed out: '+label);await new Promise(resolve=>setTimeout(resolve,25));}}
const profile=path.join(temporary,'profile'),events=[];let control,controlServer;
if(retained) {
 const hosts=path.join(profile,'NativeMessagingHosts'),audioRoot=path.join(temporary,'recordings'),socket=path.join(temporary,'control.sock');
 await mkdir(hosts,{recursive:true});await mkdir(audioRoot);
 controlServer=createSocketServer(peer=>{control=peer;let pending='';peer.setEncoding('utf8');peer.on('data',data=>{pending+=data;for(let end;(end=pending.indexOf('\n'))>=0;){events.push(JSON.parse(pending.slice(0,end)));pending=pending.slice(end+1)}})});
 await new Promise(resolve=>controlServer.listen(socket,resolve));
 for(const [name,fixture]of [['io.codemyriad.cassini','browser_host_fixture.py'],['io.codemyriad.cassini.control','control_host_fixture.py']]) {
  const executable=path.join(temporary,name+'.py');
  await writeFile(executable,`#!/usr/bin/env python3\nimport os,sys\nos.environ['CASSINI_LIBRARY_DIR']=${JSON.stringify(audioRoot)}\nos.environ['CASSINI_CONTROL_SOCKET']=${JSON.stringify(socket)}\nsys.argv=[${JSON.stringify(path.join(root,'tests',fixture))},${JSON.stringify(audioRoot)}]\nexec(compile(open(sys.argv[0]).read(),sys.argv[0],'exec'))\n`);await chmod(executable,0o700);
  await writeFile(path.join(hosts,name+'.json'),JSON.stringify({name,description:'Private BFCache fixture',path:executable,type:'stdio',allowed_origins:[`chrome-extension://${id}/`]}));
 }
}
async function command(requestId,action,tabId) {
 control.write(JSON.stringify({type:'command',requestId,action,tabId,independent:true,...(action==='start'?{mode:'tracks',keepOnNavigation:true,captureLocal:false,microphone:false}:{})})+'\n');
 const result=await observed(()=>events.find(e=>e.type==='result'&&e.requestId===requestId),'native '+action+' result');
 assert.equal(result.ok,true,JSON.stringify(result));return result;
}
let context,browserSession,page,worker,popup,remote,sessionId,nextId=0;const logs=[],pageErrors=[];
const report={version:manifest.version,mode};
try {
 context=await chromium.launchPersistentContext(profile,{
  channel:'chromium',headless:true,ignoreDefaultArgs:['--disable-back-forward-cache'],
  args:[`--disable-extensions-except=${extension}`,`--load-extension=${extension}`,`--allowlisted-extension-id=${id}`,
   '--enable-features=DisconnectExtensionMessagePortWhenPageEntersBFCache','--disable-audio-output','--autoplay-policy=no-user-gesture-required','--allow-loopback-in-peer-connection','--disable-features=WebRtcHideLocalIpsWithMdns']
 });
 worker=context.serviceWorkers()[0]||await context.waitForEvent('serviceworker');
 assert.equal(new URL(worker.url()).host,id);
 browserSession=await context.browser().newBrowserCDPSession();
 const targets=(await browserSession.send('Target.getTargets')).targetInfos;
 const target=targets.find(t=>t.type==='service_worker'&&t.url===worker.url());assert.ok(target);
 ({sessionId}=await browserSession.send('Target.attachToTarget',{targetId:target.targetId,flatten:false}));
 browserSession.on('Target.receivedMessageFromTarget',event=>{
  if(event.sessionId!==sessionId)return;const message=JSON.parse(event.message);
  if(message.method==='Log.entryAdded')logs.push(message.params.entry.text);
  if(message.method==='Runtime.consoleAPICalled')logs.push(message.params.args.map(a=>a.value??a.description??'').join(' '));
  if(message.method==='Runtime.exceptionThrown')logs.push(message.params.exceptionDetails.exception?.description||message.params.exceptionDetails.text);
 });
 async function call(method,params={}){
  const requestId=++nextId;
  const reply=new Promise((resolve,reject)=>{
   const timer=setTimeout(()=>{browserSession.off('Target.receivedMessageFromTarget',receive);reject(Error('No worker reply: '+method));},10000);
   function receive(event){if(event.sessionId!==sessionId)return;const m=JSON.parse(event.message);if(m.id!==requestId)return;
    clearTimeout(timer);browserSession.off('Target.receivedMessageFromTarget',receive);m.error?reject(Error(JSON.stringify(m.error))):resolve(m.result);}
   browserSession.on('Target.receivedMessageFromTarget',receive);
  });
  await browserSession.send('Target.sendMessageToTarget',{sessionId,message:JSON.stringify({id:requestId,method,params})});return reply;
 }
 await call('Runtime.enable');await call('Log.enable');
 await worker.evaluate(()=>{
  console.warn('cassini BFCache worker log probe');
  globalThis.fixtureConnections=[];globalThis.fixtureNativePackets=[];globalThis.fixtureNativeConnections=0;
  const nativeConnect=chrome.runtime.connectNative.bind(chrome.runtime);
  chrome.runtime.connectNative=name=>{
   const port=nativeConnect(name);
   if(name==='io.codemyriad.cassini') {
    fixtureNativeConnections++;const post=port.postMessage.bind(port);
    port.postMessage=packet=>{fixtureNativePackets.push({type:packet.type,captureId:packet.captureId,failureReason:packet.failureReason,cause:packet.cause});return post(packet)};
   }
   return port;
  };
  chrome.runtime.onConnect.addListener(port=>{
   if(port.name!=='cassini-frame'||port.sender.frameId!==0)return;
   const row={tabId:port.sender.tab.id,documentId:port.sender.documentId,ready:false,disconnected:false};fixtureConnections.push(row);
   // Observe native lifecycle/handshake only; never read lastError here.
   // An observer read would mask the unchecked receiving-callback regression.
   port.onMessage.addListener(m=>{if(m.type==='relayReady')row.ready=true});
   port.onDisconnect.addListener(()=>{row.disconnected=true});
  });
 });
 await observed(()=>logs.some(s=>s.includes('cassini BFCache worker log probe')),'worker console observer');
 page=await context.newPage();page.on('pageerror',error=>pageErrors.push(error.message));
 const pageSession=await context.newCDPSession(page);await pageSession.send('Page.enable');
 pageSession.on('Page.backForwardCacheNotUsed',value=>{report.cacheNotUsed=value});
 const url=`http://127.0.0.1:${server.address().port}/meeting`;
 await page.goto(url);
 const initial=await observed(()=>worker.evaluate(()=>fixtureConnections.find(r=>r.ready)),'initial relay handshake');
 const document=await page.evaluate(()=>fixtureDocument);report.initial=initial;report.initialDocument=document;
 if(retained){popup=await context.newPage();await popup.goto(`chrome-extension://${id}/popup.html`);}
 const status=()=>popup.evaluate(tabId=>chrome.runtime.sendMessage({target:'background',type:'status',tabId}),initial.tabId);
 let started;
 if(retained) {
  // Explicit native Start with a real connected receive slot. It has no RTP yet,
  // so the page allocates no capture context that could block BFCache eligibility.
  remote=await context.newPage();await remote.goto(`http://127.0.0.1:${server.address().port}/peer`);
  await connectReceiveSlots(page,remote,[null]);
  await observed(()=>events.filter(e=>e.type==='meetings').at(-1)?.meetings.some(m=>m.tabId===initial.tabId&&m.sourceCount===1),'real receiving-slot inventory');
  started=await command('bfcache-start','start',initial.tabId);report.started=started.status;
  assert.equal((await status()).recording,true);
  // Cache eligibility requires quiescent page APIs. Native session retention is
  // still real; this is not a claim of uninterrupted PCM from a frozen page.
  await page.evaluate(()=>topologyPeer.close());
  await remote.evaluate(async()=>{topologyRemote.peer.close();await topologyRemote.audio.close()});
 }
 // POST creates a distinct document while preserving the normalized call URL.
 // A different call address would intentionally end the capture under policy.
 if(retained){await page.getByRole('button',{name:'Navigate away'}).click();await page.waitForURL(url+'#away',{waitUntil:'commit'});}
 else {await page.getByRole('link',{name:'Navigate away'}).click();await page.waitForURL('**/away',{waitUntil:'commit'});}
 await page.waitForFunction(original=>fixtureDocument!==original,document);
 await observed(()=>worker.evaluate(()=>fixtureConnections.length>=2),'away-page relay');
 await observed(()=>worker.evaluate(initial=>fixtureConnections.find(r=>r.documentId===initial.documentId)?.disconnected,initial),'cached-page port closes in Chrome');
 if(retained) {
  report.cachedStatus=await status();assert.equal(report.cachedStatus.recording,true);
  assert.equal(report.cachedStatus.captureId,started.status.captureId);assert.equal(report.cachedStatus.path,started.status.path);
  assert.equal(report.cachedStatus.timelineOriginMs,started.status.timelineOriginMs);assert.equal(report.cachedStatus.callGeneration,started.status.callGeneration);
  assert.equal(events.some(e=>e.type==='status'&&e.sessions?.some(s=>s.stopping)),false,'cache entry must not stop the retained capture');
 }
 await page.goBack({waitUntil:'commit'});await page.waitForURL(url,{waitUntil:'commit'});
 const restored=await observed(()=>page.evaluate(original=>{
  const shows=fixtureLifecycle.filter(e=>e.type==='pageshow');
  return fixtureDocument!==original||shows.length>=2?{document:fixtureDocument,lifecycle:fixtureLifecycle,sameConstructor:RTCPeerConnection===fixtureConstructor,notRestoredReasons:performance.getEntriesByType('navigation')[0]?.notRestoredReasons?.toJSON?.()}:null;
 },document),'actual persisted pageshow');
 report.restored=restored;
 assert.equal(restored.document,document,'history must restore the same document, not reload');
 assert.equal(restored.lifecycle.at(-1).type,'pageshow');assert.equal(restored.lifecycle.at(-1).persisted,true,'must actually restore from BFCache');
 assert.equal(restored.sameConstructor,true,'cached MAIN hook must not be wrapped again');
 const connections=await observed(()=>worker.evaluate(initial=>{
  const rows=fixtureConnections.filter(r=>r.tabId===initial.tabId&&r.documentId===initial.documentId);
  return rows.length>=2&&rows.at(-1).ready?rows:null;
 },initial),'new relay handshake in the same browser document');
 assert.equal(connections.length,2,'one replacement relay after one BFCache restore');report.connections=connections;
 // This readiness probe runs only AFTER observing the fresh handshake, so it
 // cannot repair a missing pageshow reconnection and turn this test green.
 report.bridge=await worker.evaluate(tabId=>chrome.tabs.sendMessage(tabId,{type:'cassini-connect'}),initial.tabId);
 assert.equal(report.bridge.connected,true);
 if(!popup){popup=await context.newPage();await popup.goto(`chrome-extension://${id}/popup.html`);}
 report.status=await status();
 if(retained) {
  assert.equal(report.status.recording,true);assert.equal(report.status.captureId,started.status.captureId);assert.equal(report.status.path,started.status.path);
  assert.equal(report.status.timelineOriginMs,started.status.timelineOriginMs);assert.equal(report.status.callGeneration,started.status.callGeneration);
  await connectReceiveSlots(page,remote,[[440]]);
  const manifestFile=path.join(started.status.path,'session.json');
  report.recorded=await observed(async()=>{const m=JSON.parse(await readFile(manifestFile,'utf8'));return m.tracks.some(t=>t.frames>=t.sampleRate)&&m},'actual PCM after persisted restore');
  assert.equal((await status()).recording,true,'restored capture must still be running');
  assert.equal(events.some(e=>e.type==='status'&&e.sessions?.some(s=>s.stopping)),false,'no automatic Stop or loss during cache/restore');
  report.nativeBeforeStop=await worker.evaluate(()=>({connections:fixtureNativeConnections,packets:fixtureNativePackets}));
  assert.equal(report.nativeBeforeStop.connections,1);assert.equal(report.nativeBeforeStop.packets.filter(p=>p.type==='start').length,1);
  assert.equal(report.nativeBeforeStop.packets.some(p=>p.type==='stop'||p.failureReason==='lost'),false,'BFCache must not cause a native Stop or reported capture loss');
  await command('bfcache-stop','stop',initial.tabId);
  report.final=JSON.parse(await readFile(manifestFile,'utf8'));assert.equal(report.final.state,'completed');
  assert.ok(report.final.tracks.length>0);assert.ok(report.final.tracks.every(t=>t.kind==='remote'&&t.frames>0));
  const pcm=await readFile(path.join(started.status.path,report.final.tracks[0].file));
  assert.ok(pcm.subarray(44).some(byte=>byte!==0),'real generated remote PCM must reach the retained native folder');
  const results=events.filter(e=>e.type==='result'&&e.status?.path===started.status.path);
  assert.equal(results.filter(e=>e.requestId==='bfcache-start').length,1);assert.equal(results.filter(e=>e.requestId==='bfcache-stop').length,1);
 } else {assert.equal(report.status.recording,false);assert.equal(report.status.browserHealth.state,'idle');}
 assert.deepEqual(pageErrors,[]);
 assert.deepEqual(logs.filter(s=>/Unchecked runtime\.lastError/i.test(s)),[],'receiving endpoint must consume the BFCache callback error');
 console.log('PASS real BFCache '+mode+': persisted pageshow, same document/hook, fresh relay, preserved session policy and no unchecked port error');
} finally {
 report.logs=logs;report.pageErrors=pageErrors;report.controlEvents=events;
 report.native=await worker?.evaluate(()=>({connections:fixtureNativeConnections,packets:fixtureNativePackets})).catch(error=>({error:error.message}));
 report.allConnections=await worker?.evaluate(()=>fixtureConnections).catch(error=>({error:error.message}));
 if(page&&!page.isClosed())report.lastDocument=await page.evaluate(()=>({url:location.href,document:window.fixtureDocument,lifecycle:window.fixtureLifecycle})).catch(error=>({error:error.message}));
 await mkdir(path.join(root,'artifacts'),{recursive:true});await writeFile(path.join(root,retained?'artifacts/bfcache-recording.json':'artifacts/bfcache.json'),JSON.stringify(report,null,2));
 await browserSession?.detach().catch(()=>{});await context?.close();control?.destroy();if(controlServer)await new Promise(resolve=>controlServer.close(resolve));await new Promise(resolve=>server.close(resolve));await rm(temporary,{recursive:true,force:true});
}

}
