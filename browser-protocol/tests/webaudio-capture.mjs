// D74 real passive-media negatives, folded from PR75 and the supplied Chromium
// reproduction. No RTC objects/reports, media elements or lifecycle events are
// mocked. Negative admission only: no explicit capture request before the D73
// contract is implemented. Mixed-feed sites never capture browser audio.
import assert from 'node:assert/strict';
import {createHash,generateKeyPairSync} from 'node:crypto';
import {createServer} from 'node:http';
import {cp,mkdir,mkdtemp,readFile,rm,writeFile} from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {execFile} from 'node:child_process';
import {promisify} from 'node:util';
const {chromium}=await import(process.env.PLAYWRIGHT_MODULE||'playwright');
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const admissionCases=JSON.parse(await readFile(path.join(root,'tests/fixtures/webaudio-admission-cases.json'),'utf8'));
const args=process.argv.slice(2);
assert.ok(args.every(arg=>['--observe-only','--negative-cases'].includes(arg)||arg.startsWith('--fixture=')),'Unknown argument');
if(!args.length||args.includes('--negative-cases')) {
 assert.ok(!args.length||(args.length===1&&args[0]==='--negative-cases'),'Run the negative suite without other options');
 for(const fixture of admissionCases.negativeCases) {
  const {stdout}=await promisify(execFile)(process.execPath,[fileURLToPath(import.meta.url),'--observe-only','--fixture='+fixture.id],{env:process.env});
  process.stdout.write(stdout);
 }
 console.log('Generic call evidence stays unknown for passive media; no hostname admission or graph capture.');
 process.exit(0);
}
const fixtureId=args.find(arg=>arg.startsWith('--fixture='))?.slice('--fixture='.length)||'media-only-worklet';
const fixture=admissionCases.negativeCases.find(item=>item.id===fixtureId);
assert.ok(fixture,'Unknown fixture '+fixtureId);
assert.ok(args.includes('--observe-only'),'Individual fixtures are negative observation only');
const temporary=await mkdtemp(path.join(os.tmpdir(),'cassini-webaudio-'));
const extension=path.join(temporary,'extension');
const report={scope:'media-only admission negative; not capture or WhatsApp compatibility proof',fixture:fixture.id};
let context,server,worker,popup;
try {
 await cp(path.resolve(process.env.CASSINI_TEST_EXTENSION||path.join(root,'extension')),extension,{recursive:true});
 const manifestFile=path.join(extension,'manifest.json');
 const manifest=JSON.parse(await readFile(manifestFile,'utf8'));
 const key=generateKeyPairSync('rsa',{modulusLength:2048}).publicKey.export({type:'spki',format:'der'});
 manifest.key=key.toString('base64');await writeFile(manifestFile,JSON.stringify(manifest));
 const id=[...createHash('sha256').update(key).digest('hex').slice(0,32)].map(c=>String.fromCharCode(97+parseInt(c,16))).join('');
 assert.notEqual(id,'pcafdaahfhjiolfpfnlhmljfgoabbaij');
 report.extensionVersion=manifest.version;
 const processor=`class Tone extends AudioWorkletProcessor {
 constructor(){super();this.frame=0;}
 process(inputs,outputs){for(const channel of outputs[0])for(let i=0;i<channel.length;i++)channel[i]=.2*Math.sin(2*Math.PI*440*(this.frame+i)/sampleRate);this.frame+=outputs[0][0].length;return true;}
 }registerProcessor('tone',Tone);`;
 const html=`<!doctype html><title>${fixture.title}</title><main>${fixture.description}</main>
 <button id="start">Start generated page audio</button>${fixture.mediaElement?`<${fixture.mediaElement} id="player" controls muted playsinline></${fixture.mediaElement}>`:""}<script>
 document.querySelector('#start').onclick=async()=>{try{
 window.context=new AudioContext();const moduleUrl=URL.createObjectURL(new Blob([${JSON.stringify(processor)}],{type:'text/javascript'}));
 try{await context.audioWorklet.addModule(moduleUrl)}finally{URL.revokeObjectURL(moduleUrl)}
 window.node=new AudioWorkletNode(context,'tone');window.analyser=context.createAnalyser();
 node.connect(context.destination);node.connect(analyser);await context.resume();
 const player=document.querySelector('#player');if(player){
  window.mediaOutput=context.createMediaStreamDestination();node.connect(mediaOutput);
  player.srcObject=mediaOutput.stream;await player.play();
 }
 window.started=true;
 }catch(error){window.fixtureError=String(error);throw error}};</script>`;
 server=createServer((request,response)=>{response.setHeader('Content-Type',request.url==='/tone.js'?'text/javascript':'text/html');response.end(request.url==='/tone.js'?processor:html)});
 await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
 context=await chromium.launchPersistentContext(path.join(temporary,'profile'),{
  channel:'chromium',headless:true,args:[`--disable-extensions-except=${extension}`,`--load-extension=${extension}`,'--disable-audio-output','--autoplay-policy=no-user-gesture-required']
 });
 worker=context.serviceWorkers()[0]||await context.waitForEvent('serviceworker');
 assert.equal(new URL(worker.url()).host,id);
 // Observe production native packets in memory; never connect to an installed
 // host or write a user recording. No discovery/graph/RTC behavior is replaced.
 await worker.evaluate(()=>{
  globalThis.fixturePackets=[];globalThis.fixtureNativeConnections=0;
  chrome.runtime.connectNative=name=>{
   fixtureNativeConnections++;
   const event=()=>{const listeners=[];return {addListener:fn=>listeners.push(fn),emit:m=>listeners.forEach(fn=>fn(m))}};
   const port={onMessage:event(),onDisconnect:event(),closed:false,
    postMessage(packet){
     fixturePackets.push(packet);
     queueMicrotask(()=>port.onMessage.emit(packet.type==='start'?{type:'started',path:'/fixture/Page audio',features:packet.features||[]}:
      packet.type==='stop'?{type:'stopped',path:'/fixture/Page audio'}:{type:'ack'}));
    },disconnect(){if(!port.closed){port.closed=true;port.onDisconnect.emit()}}
   };if(name==='io.codemyriad.cassini.control')globalThis.fixtureControlPort=port;return port;
  };
 });
 await context.addInitScript(()=>{
  globalThis.discovery=[];
  addEventListener('message',event=>{if(event.source===window&&event.data?.channel==='cassini-page'&&event.data.type==='discovery')discovery.push(event.data)});
 });
 const page=await context.newPage();const pageErrors=report.pageErrors=[];page.on('pageerror',error=>pageErrors.push(error.message));
 const url=fixture.origin==='http://127.0.0.1'?`http://127.0.0.1:${server.address().port}/`:`${fixture.origin}/cassini-generated-fixture/`;
 // These HTTPS origins are completely fulfilled in the disposable browser.
 // No request reaches YouTube/WhatsApp and no account or real vendor UI is used.
 if(fixture.origin.startsWith('https:'))await context.route(fixture.origin+'/**',route=>route.fulfill({status:200,contentType:new URL(route.request().url()).pathname==='/tone.js'?'text/javascript':'text/html',body:new URL(route.request().url()).pathname==='/tone.js'?processor:html}));
 await page.goto(url);await page.waitForFunction(()=>globalThis.__cassiniPageHook&&discovery.length);
 await page.locator('#start').click();await page.waitForFunction(()=>globalThis.started||globalThis.fixtureError);
 assert.equal(await page.evaluate(()=>globalThis.fixtureError),undefined,'real AudioWorklet fixture must start');
 await page.waitForFunction(()=>{const samples=new Float32Array(analyser.fftSize);analyser.getFloatTimeDomainData(samples);return samples.reduce((sum,x)=>sum+x*x,0)/samples.length>.01});
 report.baseline=await page.evaluate(()=>{
  const samples=new Float32Array(analyser.fftSize);analyser.getFloatTimeDomainData(samples);
  return {hook:globalThis.__cassiniPageHook,contextState:context.state,rms:Math.sqrt(samples.reduce((sum,x)=>sum+x*x,0)/samples.length),discovery:discovery.at(-1),mediaElements:document.querySelectorAll('audio,video').length,mediaPlaying:document.querySelector('#player')?!document.querySelector('#player').paused:null};
 });
 assert.ok(report.baseline.rms>.1);assert.equal(report.baseline.contextState,'running');
 assert.equal(report.baseline.mediaElements,fixture.mediaElement?1:0);
 if(fixture.mediaElement)assert.equal(report.baseline.mediaPlaying,true,'real generated media element must be playing');
 assert.ok((await page.evaluate(()=>discovery)).every(m=>m.sourceCount===0&&m.heard===false),'media-only audio must never fabricate receiver count or heard evidence');
 const tabId=await worker.evaluate(async url=>(await chrome.tabs.query({})).find(tab=>tab.url===url).id,url);
 popup=await context.newPage();await popup.goto(`chrome-extension://${id}/popup.html`);
 async function observed(check,label){const deadline=Date.now()+15000;while(true){const value=await check();if(value)return value;assert.ok(Date.now()<deadline,'Timed out: '+label);await new Promise(resolve=>setTimeout(resolve,25));}}
 const status=()=>popup.evaluate(tabId=>chrome.runtime.sendMessage({target:'background',type:'status',tabId}),tabId);
 report.before=await status();
 assert.equal(report.before.recording,false);
 await observed(()=>worker.evaluate(()=>fixturePackets.some(p=>p.type==='meetings')&&fixtureControlPort!=null),'real control connection inventory');
 report.beforePackets=await worker.evaluate(()=>fixturePackets);
 assert.equal(report.beforePackets.some(p=>p.type==='start'),false,'page audio alone must never automatically Start');
 assert.equal(report.beforePackets.some(p=>p.type==='audio'),false,'no PCM from passive media');
 const inventories=report.beforePackets.filter(p=>p.type==='meetings');
 assert.ok(inventories.length>0,'observe actual control inventory');
 assert.ok(inventories.every(p=>p.meetings.length===0),'media-only graph must not publish a meeting');
 assert.deepEqual(pageErrors,[]);
 await worker.evaluate(()=>fixtureControlPort.onMessage.emit({type:'configure',capabilities:['call-signals-v1','verified-auto-admission-v1']}));
 const signal=await observed(()=>worker.evaluate(()=>fixturePackets.filter(p=>p.type==='callSignals').at(-1)),'negotiated call-signal snapshot');
 report.callSignals=signal;
 const profileIdentity=await worker.evaluate(async()=> (await chrome.storage.local.get('cassiniBrowserInstance')).cassiniBrowserInstance);
 assert.equal(signal.browserInstance,profileIdentity);assert.equal(typeof profileIdentity,'string');
 const acknowledged=await worker.evaluate(()=>fixturePackets.findIndex(p=>p.type==='configured')<fixturePackets.findIndex(p=>p.type==='callSignals'));
 assert.equal(acknowledged,true,'configure acknowledged before snapshot');
 const row=await observed(()=>worker.evaluate(tabId=>fixturePackets.filter(p=>p.type==='callSignals').at(-1)?.tabs.find(row=>row.tabId===tabId),tabId),'current generic unknown-session row');
 assert.equal(row.remoteAudio,'unknown');assert.equal(row.callEvidence.state,'unknown');assert.equal(row.callEvidence.callGeneration,null);
 const browserDocument=await worker.evaluate(async tabId=>(await chrome.scripting.executeScript({target:{tabId,frameIds:[0]},world:'ISOLATED',func:()=>null}))[0].documentId,tabId);
 assert.equal(row.documentGeneration,browserDocument,'browser-owned document identity');
 report.callSignals=await worker.evaluate(()=>fixturePackets.filter(p=>p.type==='callSignals').at(-1));
 const observations=await worker.evaluate(()=>fixturePackets);
 assert.equal(observations.some(p=>p.type==='start'||p.type==='audio'),false,'negotiation and playback never start capture');
 assert.ok(observations.filter(p=>p.type==='callSignals').every(p=>p.tabs.every(row=>row.callEvidence.state==='unknown')));
 console.log('PASS '+fixture.id+': real440Hz AudioWorklet RMS>0.1, no receiver/heard evidence, empty meeting inventory and no automatic capture');
}catch(error){report.error=String(error);throw error}
finally{
 if(worker)report.finalPackets=await worker.evaluate(()=>fixturePackets).catch(()=>[]);
 // Bound metadata artifacts: do not dump generated PCM payloads.
 for(const key of ['packets','finalPackets','beforePackets'])if(report[key])report[key]=report[key].map(({pcm,...packet})=>({...packet,...(pcm?{pcmBytes:Buffer.from(pcm,'base64').length}:{})}));
 await mkdir(path.join(root,'artifacts'),{recursive:true});await writeFile(path.join(root,'artifacts',fixture.id==='media-only-worklet'?'webaudio-capture.json':'webaudio-'+fixture.id+'.json'),JSON.stringify(report,null,2));
 await context?.close();if(server?.listening)await new Promise(resolve=>server.close(resolve));await rm(temporary,{recursive:true,force:true});
}
