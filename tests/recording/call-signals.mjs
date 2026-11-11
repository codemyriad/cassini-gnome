// SPDX-License-Identifier: GPL-3.0-or-later
// D74/D80: pinned producer -> native messaging -> GNOME -> private Pulse PCM.
// Passive pages use the unmodified production collector. Mixed-route ownership
// uses an explicitly controlled trusted adapter, not real-site qualification.
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {execFile, spawn} from 'node:child_process';
import {cp, mkdir, mkdtemp, readFile, writeFile, chmod, rm} from 'node:fs/promises';
import {createWriteStream} from 'node:fs';
import {createInterface} from 'node:readline';
import path from 'node:path';
import os from 'node:os';
import {promisify} from 'node:util';
import {chromium} from 'playwright';
assert.equal(process.env.CASSINI_ISOLATED_AUDIO, '1', 'Use the private recording harness');
assert.ok(!process.env.WAYLAND_DISPLAY);
assert.match(process.env.PULSE_SERVER || '', /^unix:\/tmp\/cassini-audio-/);
const root = path.resolve(import.meta.dirname, '../..');
const artifacts = process.env.CASSINI_TEST_ARTIFACTS;
process.env.CASSINI_LIBRARY_DIR = artifacts;
const extension = process.env.CASSINI_TEST_EXTENSION || path.join(root, 'browser-extension');
const profile = await mkdtemp(path.join(os.tmpdir(), 'cassini-d74-producer-'));
const evidence = {scenarios: [], errors: [], console: []}, pending = [], tones = [];
let context, worker, ui;
async function until(check, label) {
 const deadline = Date.now() + 20000;
 while (true) {
  const value = await check(); if (value) return value;
  assert.ok(Date.now() < deadline, 'Timed out: ' + label);
  await new Promise(resolve => setTimeout(resolve, 50));
 }
}
function call(packet = {action:'status'}) {
 return new Promise((resolve, reject) => {
  const timer = setTimeout(() => reject(Error('GNOME UI request timed out')), 12000);
  pending.push(value => {clearTimeout(timer); value.error ? reject(Error(value.error)) : resolve(value)});
  ui.stdin.write(JSON.stringify(packet) + '\n');
 });
}
async function automatic(enabled) {
 await call({action:'show-settings'});
 await until(async () => (await call()).settingsReady, 'Settings reads the broker preference');
 await call({action:'auto',enabled});
 await until(async () => (await call()).auto === enabled, 'opt-in applied');
}
async function samples(directory) {
 const manifest = JSON.parse(await readFile(path.join(directory,'session.json'))), tracks = [];
 for (const track of manifest.tracks) {
  const bytes = await readFile(path.join(directory,track.file));
  tracks.push({...track,bytes,frames:(bytes.length-44)/(2*track.channels)});
 }
 return {manifest,tracks};
}
function power(track, hz) {
 const count = Math.min(track.frames,48000), start = track.frames-count;
 let real=0,imaginary=0;
 for(let i=0;i<count;i++) {
  const sample=track.bytes.readInt16LE(44+(start+i)*track.channels*2)/32768;
  real+=sample*Math.cos(2*Math.PI*hz*i/track.sampleRate);
  imaginary+=sample*Math.sin(2*Math.PI*hz*i/track.sampleRate);
 }
 return (real*real+imaginary*imaginary)/(count*count);
}
async function recording() {
 const status = await until(async () => (await call()).statuses.find(s=>s.recording && s.path && !s.starting), 'system recording');
 await until(async () => (await samples(status.path)).tracks.some(t=>t.kind==='system' && t.frames>=48000), 'one second of actual system PCM');
 return status.path;
}
async function settled(directory) {
 await until(async () => (await samples(directory)).manifest.state !== 'recording', 'normal Stop finalizes WAVs');
 const audio = await samples(directory);
 assert.equal(audio.manifest.state,'completed');
 assert.equal(audio.manifest.events.filter(e=>e.type==='end').length,1);
 return audio;
}
async function facts(page, state, generation) {
 const tab = await worker.evaluate(async url => (await chrome.tabs.query({})).find(t=>t.url===url).id,page.url());
 await worker.evaluate(async ({tab,state,generation}) => {
  fixture.facts.set(tab,{state,reason:{active:'activeCall',inactive:'callEnded',unknown:'unverified'}[state],callGeneration:generation});
  await fixture.refresh();
 },{tab,state,generation});
 await until(async () => (await call()).callSignals.some(p=>p.browserInstance.endsWith(':trusted-fixture') && p.tabs.some(t=>t.tabId===tab && t.callEvidence.state===state && t.callEvidence.callGeneration===generation)), 'real producer evidence reaches GNOME');
 return tab;
}
try {
 await mkdir(artifacts,{recursive:true});
 const manifest=JSON.parse(await readFile(path.join(extension,'manifest.json')));
 const legacyCompanion=manifest.version==='0.2.25';
 evidence.companionVersion=manifest.version;
 const id=[...createHash('sha256').update(Buffer.from(manifest.key,'base64')).digest('hex').slice(0,32)].map(c=>String.fromCharCode(97+parseInt(c,16))).join('');
 await mkdir(path.join(profile,'NativeMessagingHosts'),{recursive:true});
 for (const [name,module] of [['io.codemyriad.cassini.control','browser_control_host'],['io.codemyriad.cassini','browser_host']]) {
  const script=path.join(profile,name+'.py');
  await writeFile(script,`#!/usr/bin/python3\nimport sys\nsys.path.insert(0,${JSON.stringify(root+'/src')})\nsys.stderr=open(${JSON.stringify(path.join(artifacts,name+'.log'))},'a',buffering=1)\nfrom cassini_talk.${module} import main\nsys.exit(main())\n`);
  await chmod(script,0o700);
  await writeFile(path.join(profile,'NativeMessagingHosts',name+'.json'),JSON.stringify({name,description:'Private D74 app proof',path:script,type:'stdio',allowed_origins:[`chrome-extension://${id}/`]}));
 }
 ui=spawn('/usr/bin/python3',[path.join(root,'tests/recording/browser-ui.py'),'--build',process.env.CASSINI_TEST_BUILD],{stdio:['pipe','pipe','pipe']});
 ui.stderr.pipe(createWriteStream(path.join(artifacts,'gnome-ui.log')));
 createInterface({input:ui.stdout}).on('line',line=>{try {pending.shift()?.(JSON.parse(line))}catch {evidence.console.push(line)}});
 await until(async () => (await call()).shortcutBound,'private GNOME app ready');
 const virtualMic=path.join(root,'tests/recording/virtual-microphone.py');
 evidence.tonesReady={};
 for(const [kind,hz,channels] of [['microphone',660,1],['desktop',440,2]]) {
  const sink=`cassini_${kind}`;
  const tone=spawn('/usr/bin/python3',[virtualMic,'produce','--sink',sink,'--hz',String(hz),'--channels',String(channels)],{stdio:['ignore','ignore','pipe']});
  tone.stderr.pipe(createWriteStream(path.join(artifacts,kind+'.log')));tones.push(tone);
  try {
   const {stdout}=await promisify(execFile)('/usr/bin/python3',[virtualMic,'probe','--sink',sink,'--hz',String(hz)],{timeout:18000,killSignal:'SIGKILL'});
   evidence.tonesReady[kind]=JSON.parse(stdout);
  } catch (error) {
   throw new Error(`Virtual ${kind} fixture failed before browser capture: ${error.message}. See ${kind}.log and pulse-server.log.`,{cause:error});
  }
 }
 const fixtureExtension=path.join(profile,'fixture-extension');
 await cp(extension,fixtureExtension,{recursive:true});
 await cp(path.join(root,'tests/recording/call-signals-worker.js'),path.join(fixtureExtension,'call-signals-worker.js'));
 manifest.background.service_worker='call-signals-worker.js';
 await writeFile(path.join(fixtureExtension,'manifest.json'),JSON.stringify(manifest));
 context=await chromium.launchPersistentContext(profile,{channel:'chromium',headless:false,ignoreDefaultArgs:['--mute-audio'],args:[`--disable-extensions-except=${fixtureExtension}`,`--load-extension=${fixtureExtension}`,'--no-sandbox','--ozone-platform=x11','--autoplay-policy=no-user-gesture-required']});
 const cases=JSON.parse(await readFile(path.join(root,'browser-protocol/tests/fixtures/webaudio-admission-cases.json'))).negativeCases;
 const body=media=>`<!doctype html><title>D74 generated fixture</title><button id="play">Play generated audio</button>${media?`<${media} id="player" muted playsinline></${media}>`:''}<script>
 play.onclick=async()=>{window.audio=new AudioContext();
 const processor="class Tone extends AudioWorkletProcessor{constructor(){super();this.frame=0}process(inputs,outputs){for(const channel of outputs[0])for(let i=0;i<channel.length;i++)channel[i]=Math.sin(2*Math.PI*880*(this.frame+i)/sampleRate);this.frame+=outputs[0][0].length;return true}}registerProcessor('tone',Tone)";
 const module=URL.createObjectURL(new Blob([processor],{type:'text/javascript'}));
 try{await audio.audioWorklet.addModule(module)}finally{URL.revokeObjectURL(module)}
 window.tone=new AudioWorkletNode(audio,'tone');
 window.gain=audio.createGain();gain.gain.value=.2;tone.connect(gain);gain.connect(audio.destination);
 window.analyser=audio.createAnalyser();gain.connect(analyser);
 const player=document.querySelector('#player');if(player){window.output=audio.createMediaStreamDestination();gain.connect(output);player.srcObject=output.stream;await player.play()}
 await audio.resume();window.playing=true}</script>`;
 await context.route('**/*',route=>{
  const fixture=cases.find(c=>route.request().url().includes('/'+c.id));
  return route.fulfill({status:200,contentType:'text/html',body:body(fixture?.mediaElement)});
 });
 worker=context.serviceWorkers()[0]||await context.waitForEvent('serviceworker');
 worker.on('console',m=>evidence.console.push(m.text()));
 await until(async () => (await call()).connected>0,'real companion native handshake');
 await automatic(true);
 const passive=await context.newPage();
 passive.on('pageerror',error=>evidence.errors.push(error.message));
 await passive.addInitScript(()=>{globalThis.discovery=[];addEventListener('message',event=>{if(event.source===window&&event.data?.channel==='cassini-page'&&event.data.type==='discovery')discovery.push(event.data)})});
 for(const fixture of cases) {
  await passive.goto(fixture.origin+'/'+fixture.id);
  await passive.locator('#play').click();
  await passive.waitForFunction(()=>globalThis.playing&&globalThis.__cassiniPageHook&&discovery.length&&(()=>{const samples=new Float32Array(analyser.fftSize);analyser.getFloatTimeDomainData(samples);return samples.reduce((sum,x)=>sum+x*x,0)/samples.length>.01})());
  assert.ok((await passive.evaluate(()=>discovery)).every(row=>row.sourceCount===0&&row.heard===false));
  assert.equal(await passive.evaluate(()=>document.querySelector('#player')?.paused??false),false);
  const tab=await worker.evaluate(async url=>(await chrome.tabs.query({})).find(t=>t.url===url).id,passive.url());
  const signals=await until(async()=>{
   const rows=(await call()).callSignals.flatMap(p=>p.tabs);
   return !legacyCompanion || fixture.origin==='https://web.whatsapp.com' ? rows.find(t=>t.tabId===tab&&t.url===passive.url()) : !rows.some(t=>t.tabId===tab)&&rows;
  },'current unmodified producer snapshot for '+fixture.id);
  if(legacyCompanion && fixture.origin==='https://web.whatsapp.com') {
   assert.equal(signals.remoteAudio,'mixed');assert.equal(signals.callEvidence.state,'unknown');assert.equal(signals.callEvidence.reason,'unverified');
  }
  if (!legacyCompanion) {
   assert.equal(signals.callEvidence.state,'unknown');
   assert.equal(signals.callEvidence.adapter.name,'webrtc-interactive');
  }
  assert.equal((await call()).statuses.some(s=>s.recording),false,'passive '+fixture.id+' must not auto-start');
  assert.equal((await call()).meetings.length,0,'passive audio must not become an RTP meeting');
  evidence.scenarios.push({name:'unmodified producer passive negative: '+fixture.id,signal:signals});
 }
 await automatic(false);
 const first=await context.newPage();await first.goto('https://fixture.example/first');
 const second=await context.newPage();await second.goto('https://fixture.example/second');
 // The pinned producer and AppControl run in the actual extension worker. The
 // ONLY synthetic input is an extension-owned adapter's validated evidence.
 // Chrome tabs, browser document IDs, native messaging, broker and PCM are real.
 await worker.evaluate(async legacyCompanion => {
  const {AppControl,SiteSignalAdapters,CallSignalProducer}=globalThis.fixtureApi;
  const instance=(await chrome.storage.local.get('cassiniBrowserInstance')).cassiniBrowserInstance+':trusted-fixture';
  globalThis.fixture={facts:new Map(),frames:new Map(),commands:[],packets:[],instance};
  const adapters=new SiteSignalAdapters([{name:'synthetic-site-fixture',version:'1',hostname:'fixture.example',remoteAudio:'mixed',evidence:document=>({...fixture.facts.get(document.tabId),documentGeneration:document.documentGeneration})}]);
  fixture.producer=new CallSignalProducer({identity:()=>instance,tabs:chrome.tabs,frames:()=>fixture.frames.values(),current:frame=>fixture.frames.get(frame.tabId)===frame,adapters});
  fixture.refresh=async()=>{
   const next=new Map();
   for(const tab of await chrome.tabs.query({}))if(tab.url?.startsWith('https://fixture.example/')) {
    const [result]=await chrome.scripting.executeScript({target:{tabId:tab.id,frameIds:[0]},world:'ISOLATED',func:()=>null});
    next.set(tab.id,{tabId:tab.id,frameId:0,browserDocumentId:result.documentId,browserUrl:tab.url});
   }
   fixture.frames=next;fixture.control.publish();
  };
  fixture.connect=()=>{
   fixture.control=new AppControl({runtime:chrome.runtime,alarms:chrome.alarms,capabilities:['call-signals-v1','heartbeat-v1',...(!legacyCompanion?['verified-auto-admission-v1']:[])],signalSnapshot:async options=>{
    const snapshot=await fixture.producer.snapshot();
    // Controlled mixed-route contract input, explicitly separate from the real
    // generic collector above. The current producer has no mixed-site adapter.
    if (!legacyCompanion && options.verified) for(const row of snapshot.tabs) row.sourceDocumentGeneration='controlled-source:'+row.documentGeneration;
    return snapshot;
   },snapshot:async()=>[{type:'meetings',meetings:[]},{type:'status',recording:false,tabId:null}],command:async()=>{throw Error('Signal-only recording must never request browser audio')}});
   const send=fixture.control.send.bind(fixture.control),receive=fixture.control.receive.bind(fixture.control);
   fixture.control.send=(port,packet)=>{fixture.packets.push(packet);return send(port,packet)};
   fixture.control.receive=(port,packet)=>{if(packet.type==='command')fixture.commands.push(packet);return receive(port,packet)};
   fixture.control.start();
  };
  fixture.connect();await fixture.refresh();
 },legacyCompanion);
 const firstTab=await facts(first,'active','call-A');
 assert.equal((await call()).statuses.some(s=>s.recording),false,'positive evidence still needs opt-in');
 await automatic(true);
 if (legacyCompanion) {
  // The trusted adapter supplies call-signals-v1, not verified admission. An
  // active legacy fact cannot replace the negotiated feature and identity join.
  const state=await call();
  const peer=state.browserPeers.find(p=>p.id===state.callSignals.find(p=>p.browserInstance.endsWith(':trusted-fixture')).peer);
  assert.deepEqual(peer.capabilities.toSorted(),['call-signals-v1','heartbeat-v1']);
  assert.equal(peer.automaticVerified,false);
  assert.equal(state.auto,true);
  assert.deepEqual(state.meetings,[]);
  assert.deepEqual(state.statuses,[],'legacy mixed evidence must not start a broker capture');
  assert.deepEqual(state.nativeCaptures,[],'legacy mixed evidence must not start a native capture');
  assert.equal(state.recordStartPending,false);
  assert.equal(state.error,null);
  const configured=await worker.evaluate(()=>fixture.packets.filter(p=>p.type==='configured').at(-1));
  assert.deepEqual(configured.capabilities,['call-signals-v1']);
  evidence.scenarios.push({name:'legacy-mixed-active-fact-fails-closed',peer,configured});

  // Actual Library Record, not a direct recorder call or a fabricated proof.
  // No RTP inventory exists, so production Record chooses native system audio.
  await until(async ()=>(await call()).recordReady,'manual Record is available');
  await call({action:'record'});
  const native=await until(async ()=>(await call()).nativeCaptures.find(s=>s.recording&&!s.starting),'explicit manual system recording');
  const directory=native.path;
  await until(async ()=>(await samples(directory)).tracks.some(t=>t.kind==='system'&&t.frames>=48000),'manual system input contains real private PCM');
  const audio=await samples(directory),mic=audio.tracks.find(t=>t.kind==='microphone'),system=audio.tracks.find(t=>t.kind==='system');
  assert.ok(mic&&system&&audio.tracks.every(t=>['microphone','system'].includes(t.kind)));
  const powers={microphone660:power(mic,660),system440:power(system,440)};
  assert.ok(powers.microphone660>.001&&powers.system440>.001,JSON.stringify(powers));
  assert.equal(audio.manifest.source,'gnome');
  await facts(first,'inactive','call-A');
  await until(async ()=>(await samples(directory)).tracks.find(t=>t.kind==='system').frames>=system.frames+48000,'manual system audio continues after a legacy call end');
  assert.equal((await samples(directory)).manifest.state,'recording','legacy call ownership must not end an explicit manual recording');
  await call({action:'notification-stop'});
  const completed=await settled(directory);
  assert.equal(completed.manifest.events.at(-1).reason,'user');
  await until(async ()=>(await call()).recordReady,'manual Stop fully saved');
  assert.deepEqual(await worker.evaluate(()=>fixture.commands),[],'manual native Record does not send browser audio commands');
  assert.deepEqual(evidence.errors,[]);
  evidence.scenarios.push({name:'explicit-manual-system-record-with-legacy-companion',directory,powers,end:completed.manifest.events.at(-1)});
  console.log('PASS legacy mixed signals cannot auto-start; actual manual system/microphone PCM and explicit Stop remain available');
 } else {
 // Controlled verified mixed-route ownership, with real native PCM/transport.
 // This is not a claim that the generic producer classifies any site as mixed.
 const directory=await recording();
 const audio=await samples(directory), mic=audio.tracks.find(t=>t.kind==='microphone'),system=audio.tracks.find(t=>t.kind==='system');
 assert.ok(mic && system && audio.tracks.every(t=>['microphone','system'].includes(t.kind)));
 const powers={microphone660:power(mic,660),system440:power(system,440)};
 assert.ok(powers.microphone660>0.001 && powers.system440>0.001,JSON.stringify(powers));
 assert.equal(audio.manifest.source,'gnome');
 assert.ok(audio.manifest.events.some(e=>e.type==='othersSource'&&e.source==='system'&&e.reason==='start'));
 assert.ok(!audio.manifest.events.some(e=>e.type==='othersSource'&&['lost','missing'].includes(e.reason)));
 await until(async ()=>(await call()).library.some(m=>m.path===directory),'signal owner visible in Library');
 await call({action:'details',path:directory});
 await until(async ()=>(await call()).details?.includes('This call mixed all remote voices into one stream'),'quiet Details uses persisted route');
 assert.ok(!(await call()).details.includes('synthetic-site-fixture'), 'machine adapter ID is not a display name');
 assert.equal((await call()).notifications['recording-fallback-'+path.basename(directory)],undefined);
 await call({action:'selection',path:directory,me:false,others:true});
 await call({action:'sources-done'});
 await until(async ()=>(await samples(directory)).manifest.events.some(e=>e.type==='selection'&&!e.me&&e.others),'Me selection committed');
 const secondTab=await facts(second,'active','call-B');
 assert.equal((await call()).statuses.filter(s=>s.recording).length,1,'overlapping calls share one owner');
 assert.equal((await samples(directory)).manifest.events.filter(e=>e.type==='selection').at(-1).me,false,'new association preserves Me choice');
 const firstPeer=(await call()).callSignals.find(p=>p.browserInstance.endsWith(':trusted-fixture')).peer;
 await worker.evaluate(()=>{fixture.control.close();fixture.connect()});
 await until(async ()=>(await call()).callSignals.some(p=>p.browserInstance.endsWith(':trusted-fixture')&&p.peer!==firstPeer&&p.sequence===0&&p.tabs.some(t=>t.tabId===firstTab)), 'fresh configured replacement producer snapshot');
 assert.equal((await call()).statuses.filter(s=>s.recording).length,1);
 assert.equal((await call()).statuses.find(s=>s.recording).path,directory);
 await facts(first,'inactive','call-A');
 assert.ok((await call()).statuses.some(s=>s.recording&&s.path===directory),'one call end keeps the other association');
 await call({action:'notification-stop'});
 await settled(directory);
 await until(async ()=>(await call()).recordReady,'app observes completed global Stop');
 const stoppedPeer=(await call()).callSignals.find(p=>p.browserInstance.endsWith(':trusted-fixture')).peer;
 await worker.evaluate(()=>{fixture.control.close();fixture.connect()});
 await until(async ()=>(await call()).callSignals.some(p=>p.browserInstance.endsWith(':trusted-fixture')&&p.peer!==stoppedPeer&&p.tabs.some(t=>t.tabId===secondTab&&t.callEvidence.state==='active')),'same generation reconciles after manual Stop');
 assert.equal((await call()).statuses.some(s=>s.recording),false,'Stop suppression survives real producer reconnect');
 await facts(second,'active','call-C');
 const next=await recording();assert.notEqual(next,directory);
 const before=(await samples(next)).tracks.find(t=>t.kind==='system').frames;
 await until(async ()=>(await samples(next)).tracks.find(t=>t.kind==='system').frames>=before+48000,'system audio continues independently of call evidence');
 await facts(second,'inactive','call-C');
 const completed=await settled(next);
 assert.equal(completed.manifest.events.filter(e=>e.type==='end')[0].reason,'callEnded');
 const metadata=JSON.parse(await readFile(path.join(next,'meeting.json')));
 assert.deepEqual(metadata.recordingDetails.othersRoute,{source:'system',reason:'mixedSiteSignal',site:'synthetic-site-fixture'});
 assert.deepEqual(await worker.evaluate(()=>fixture.commands),[],'no browser Start/Stop command on the signal route');
 assert.deepEqual(evidence.errors,[]);
 evidence.scenarios.push({name:'real producer/native PCM, opt-in, shared owner, Me off, reconnect, Stop suppression, next generation, positive end during audio',directory,next,powers});
 console.log('PASS D74 verified producer -> GNOME: opt-in system PCM and ownership');
 }
} catch(error) {evidence.error=String(error);if(ui&&ui.exitCode===null)evidence.app=await call().catch(e=>({error:String(e)}));throw error}
finally {
 if(worker)evidence.producer=await worker.evaluate(()=>globalThis.fixture?.packets).catch(()=>[]);
 if(ui&&ui.exitCode===null) {
  try {await call({action:'quit'});ui.stdin.end();await new Promise(resolve=>{const timer=setTimeout(()=>{ui.kill();resolve()},4000);if(ui.exitCode!==null){clearTimeout(timer);resolve()}else ui.once('exit',()=>{clearTimeout(timer);resolve()})})} catch {ui.kill()}
 }
 await context?.close();
 for(const tone of tones){tone.kill();await new Promise(resolve=>{if(tone.exitCode!==null||tone.signalCode!==null)resolve();else tone.once('exit',resolve)})}
 await writeFile(path.join(artifacts,'call-signals-results.json'),JSON.stringify(evidence,null,2));
 await rm(profile,{recursive:true,force:true});
}
