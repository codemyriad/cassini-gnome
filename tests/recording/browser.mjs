// Real Chromium + native messaging + WebRTC. No microphone, network service,
// page hooks, audio packets, or native protocol are mocked.
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
import {createServer} from 'node:http';
import {mkdtemp, mkdir, readFile, writeFile, chmod, rm} from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import {fileURLToPath} from 'node:url';
import {chromium} from 'playwright';
import {energy} from './signal.mjs';
if (!process.env.CASSINI_ISOLATED_AUDIO || process.env.WAYLAND_DISPLAY) throw new Error('Run through tests/recording/run.sh to isolate desktop and audio.');
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const artifacts = path.resolve(process.env.CASSINI_TEST_ARTIFACTS || path.join(root, 'tests/recording/artifacts/browser'));
await mkdir(artifacts, {recursive:true});
const profile = await mkdtemp(path.join(os.tmpdir(), 'cassini-chromium-'));
const fixture = await readFile(path.join(root, 'tests/recording/fixture.html'));
const server = createServer((req,res) => {res.setHeader('Content-Type','text/html');res.end(fixture);});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
let context;
const evidence = {scenarios: [], errors: [], console: []};
const sessions = new Set();
function wav(buffer, finalized) {
  assert.equal(buffer.toString('ascii', 0, 4), 'RIFF');
  if(finalized) assert.equal(buffer.readUInt32LE(40), buffer.length - 44);
  return {rate:buffer.readUInt32LE(24), channels:buffer.readUInt16LE(22), bytes:buffer.subarray(44)};
}
async function poll(check, description, timeout=15000) {
  const deadline=Date.now()+timeout;
  while(Date.now()<deadline) {
    const value=await check();
    if(value) return value;
    await new Promise(resolve=>setTimeout(resolve,50));
  }
  throw new Error(`Timed out: ${description}`);
}
async function snapshot(directory, finalized=false) {
  const manifest=JSON.parse(await readFile(path.join(directory,'session.json')));
  const tracks=[];
  for(const track of manifest.tracks) tracks.push({...track,audio:wav(await readFile(path.join(directory,track.file)),finalized)});
  return {manifest,tracks};
}
try {
  context = await chromium.launchPersistentContext(profile, {
    channel:'chromium', headless:false,
    args:[`--disable-extensions-except=${root}/browser-extension`,`--load-extension=${root}/browser-extension`,
      '--autoplay-policy=no-user-gesture-required','--allow-loopback-in-peer-connection',
      '--disable-features=WebRtcHideLocalIpsWithMdns','--no-sandbox','--ozone-platform=x11',...(process.env.CASSINI_TALK_ONLY ? ['--use-fake-device-for-media-stream','--use-fake-ui-for-media-stream'] : [])]
  });
  const worker=context.serviceWorkers()[0] || await context.waitForEvent('serviceworker');
  worker.on('console',msg=>evidence.console.push({source:'worker',text:msg.text()}));
  const id=new URL(worker.url()).host;
  const shim=path.join(profile,'host.py');
  await writeFile(shim,`#!/usr/bin/python3\nimport importlib.util, sys, os\nopen(${JSON.stringify(path.join(artifacts,"host.pid"))},"w").write(str(os.getpid()))\nsys.stderr=open(${JSON.stringify(path.join(artifacts,'host.log'))},'a',buffering=1)\nspec=importlib.util.spec_from_file_location('host',${JSON.stringify(path.join(root,'browser-extension/native_host.py'))})\nmodule=importlib.util.module_from_spec(spec)\nspec.loader.exec_module(module)\nraise SystemExit(module.main(directory=${JSON.stringify(artifacts)},open_completed=False))\n`);
  await chmod(shim,0o700);
  await mkdir(path.join(profile,'NativeMessagingHosts'), {recursive:true});
  await writeFile(path.join(profile,'NativeMessagingHosts/com.codemyriad.cassini.json'),JSON.stringify({
    name:'com.codemyriad.cassini',description:'Cassini integration test',path:shim,type:'stdio',allowed_origins:[`chrome-extension://${id}/`]
  }));
  if (process.env.CASSINI_TALK_ONLY) {
    await (await import('./talk.mjs')).run({context,worker,id,artifacts,poll});
  } else {
  const meeting=await context.newPage(), remote=await context.newPage();
  for(const page of [meeting,remote]) {
    page.on('pageerror',e=>evidence.errors.push(e.message));
    page.on('console',msg=>evidence.console.push({source:'page',text:msg.text()}));
    await page.goto(`http://127.0.0.1:${server.address().port}/`);
    await page.click('#join');
  }
  await meeting.evaluate(()=>join(440));
  await remote.evaluate(()=>join(880));
  const offer=await meeting.evaluate(async()=>{await meeting.pc.setLocalDescription(await meeting.pc.createOffer());return meeting.description();});
  const answer=await remote.evaluate(async offer=>{await meeting.pc.setRemoteDescription(offer);await meeting.pc.setLocalDescription(await meeting.pc.createAnswer());return meeting.description();},offer);
  await meeting.evaluate(answer=>meeting.pc.setRemoteDescription(answer),answer);
  await meeting.waitForFunction(()=>meeting.pc.connectionState==='connected');
  await remote.waitForFunction(()=>meeting.pc.connectionState==='connected');
  await meeting.bringToFront();
  assert.ok((await worker.evaluate(()=>chrome.commands.getAll())).some(c=>c.name==='_execute_action' && c.shortcut==='Ctrl+Shift+9'),'extension action shortcut registered');
  // Invoke the extension's real Chrome action, granting activeTab as a user does.
  execFileSync('xdotool',['search','--onlyvisible','--name','Cassini deterministic meeting','windowfocus','--sync','key','--clearmodifiers','ctrl+shift+9']);
  await poll(async()=>(await worker.evaluate(()=>chrome.runtime.getContexts({}))).some(c=>c.contextType==='POPUP'),'real extension action');
  const popup=await context.newPage();
  await popup.goto(`chrome-extension://${id}/popup.html`);
  await meeting.bringToFront();
  async function command(type, mode) {
    const result=await popup.evaluate(({type,mode})=>chrome.runtime.sendMessage({target:'background',type,mode}),{type,mode});
    if(result.error) {console.error(await worker.evaluate(()=>chrome.tabs.query({active:true,currentWindow:true})));throw new Error(result.error);}
    if(result.path) sessions.add(result.path);
    return result;
  }
  async function stop() {
    await command('stop');
    return poll(async()=>{
      const result=await command('status');
      return !result.recording && result;
    },'Stop finalizes recording',10000);
  }
  // Deliberate idle-lifecycle test. Also force termination: attached developer
  // tooling can keep a worker alive, so elapsed idle time alone is insufficient.
  await new Promise(resolve=>setTimeout(resolve,35000));
  const lifecycle=await context.newCDPSession(meeting);
  await lifecycle.send('ServiceWorker.enable');
  await lifecycle.send('ServiceWorker.stopAllWorkers');
  await lifecycle.detach();
  const startEpoch=Date.now();
  const started=await command('start','tracks');
  assert.equal(started.recording,true);
  evidence.scenarios.push({name:'idle-and-terminated-worker-reconnect'});
  // Deliberate fault injection, not event sequencing: pause only this run's host.
  const hostPid=Number(await readFile(path.join(artifacts,'host.pid'),'utf8'));
  assert.ok(Number.isInteger(hostPid) && hostPid>1);
  process.kill(hostPid,'SIGSTOP');
  try { await new Promise(resolve=>setTimeout(resolve,3000)); }
  finally { process.kill(hostPid,'SIGCONT'); }
  assert.equal((await command('status')).recording,true,'three-second host stall survives');
  evidence.scenarios.push({name:'three-second-native-host-stall'});
  const initialLocal=await poll(async()=>{
    const captured=await snapshot(started.path);
    const track=captured.tracks.find(t=>t.kind==='local');
    return track && track.audio.bytes.length>2*48000*2 && track;
  },'local source captures two seconds before mute',10000);
  assert.ok(initialLocal,'local audio source registered');
  const beforeMute=await readFile(path.join(started.path,initialLocal.file));
  const muteFrame=(beforeMute.length-44)/(initialLocal.channels*2);
  assert.ok(muteFrame>=48000*2,'two seconds actually captured before muting');
  let state=await command('status');
  assert.equal(state.tracks,2,JSON.stringify(state));
  const muteEpoch=await meeting.evaluate(()=>{meeting.mute(true);return Date.now();});
  async function localWithDuration(index, seconds) {
    return poll(async()=>{
      const captured=await snapshot(started.path);
      const track=captured.tracks.filter(t=>t.kind==='local')[index];
      return track && track.audio.bytes.length/(2*track.channels*track.sampleRate)>=seconds && track;
    },`local source ${index} reaches ${seconds}s`);
  }
  await localWithDuration(0,muteFrame/48000+.8);
  await meeting.evaluate(()=>meeting.mute(false));
  await localWithDuration(0,muteFrame/48000+1.5);
  await meeting.evaluate(()=>meeting.replace(660));
  await localWithDuration(1,1.8);
  const removeEpoch=Date.now();
  await meeting.evaluate(()=>{
    const preview=document.createElement('audio');preview.muted=true;
    preview.srcObject=new MediaStream([meeting.sender.track]);document.body.append(preview);
    meeting.pc.removeTrack(meeting.sender);
  });
  await poll(async()=>{
    const captured=await snapshot(started.path);
    const received=captured.tracks.find(t=>t.kind==='remote');
    return received && received.audio.bytes.length/(2*received.channels*received.sampleRate)>
      (removeEpoch-startEpoch)/1000+.5;
  },'remote continues after sender removal');
  assert.equal((await command('status')).tracks,3,'removed local preview must not become a generic media recording');
  const stopEpoch=Date.now();
  const stopped=await stop();
  const first=await snapshot(stopped.path,true);
  assert.equal(first.manifest.state,'completed',JSON.stringify(first.manifest));
  assert.equal(first.tracks.length,3,'replacement creates a distinct source, not a participant identity');
  const local=first.tracks.filter(t=>t.kind==='local'), received=first.tracks.find(t=>t.kind==='remote');
  assert.equal(local.length,2); assert.ok(received);
  assert.ok(energy(local[0].audio,0.5,1.5,440).tone > .1,'local 440 Hz source');
  assert.ok(energy(local[0].audio,0.5,1.5,880).tone < .01,'no remote bleed into local source');
  assert.ok(energy(received.audio,0.5,1.5,880).tone > .08,'remote 880 Hz source');
  assert.ok(energy(received.audio,0.5,1.5,440).tone < .01,'no local bleed into remote source');
  const mutePosition=muteFrame/48000;
  const wallMutePosition=(muteEpoch-startEpoch)/1000;
  assert.ok(Math.abs(mutePosition+local[0].startOffsetMs/1000-wallMutePosition)<.4,
    `render timeline follows real elapsed time: samples=${mutePosition}, offset=${local[0].startOffsetMs}, wall=${wallMutePosition}`);
  assert.ok(energy(local[0].audio,mutePosition+.2,mutePosition+.5,440).rms < .005,'meeting mute becomes silence after the measured sample position');
  assert.ok([.3,.4,.5,.6,.7,.8].reduce((sum,start)=>sum+energy(local[1].audio,start,start+.1,660).tone,0)/6 > .1,'replacement source contains new tone');
  assert.ok(local[1].startOffsetMs > 3000,'replacement is positioned on the shared session timeline');
  // The remote and replacement sources remain alive through Stop (removed senders retain silence).
  // The original sender ends on an asynchronous discovery scan, so its wall end is not replaceEpoch.
  const continuity=[{track:local[1],end:stopEpoch},{track:received,end:stopEpoch}].map(({track,end})=>{
    const seconds=track.frames/track.sampleRate;
    const wallSeconds=(end-startEpoch-track.startOffsetMs)/1000;
    return {id:track.id,seconds,wallSeconds,error:seconds-wallSeconds};
  });
  evidence.continuity=continuity;
  for(const measure of continuity) assert.ok(Math.abs(measure.error)<.25,`audio continuity ${JSON.stringify(measure)}`);
  evidence.scenarios.push({name:'webrtc-separation-mute-replace-stop' ,path:stopped.path,tracks:first.manifest.tracks});
  console.log('PASS real WebRTC source separation, mute, replacement, timeline and stop');
  // Repeated start, then navigation: retain playable partial files with an explicit interruption state.
  const again=await command('start','tracks');
  await poll(async()=> (await snapshot(again.path)).tracks.some(t=>t.audio.bytes.length>48000*2),'audio before navigation');
  await meeting.goto(`http://127.0.0.1:${server.address().port}/?navigated`);
  const interrupted=await poll(async()=>{
    const state=await command('status');
    return !state.recording && state;
  },'navigation finalizes interrupted recording',10000);
  assert.equal(interrupted.recording,false);
  const partial=await snapshot(interrupted.path,true);
  assert.equal(partial.manifest.state,'interrupted');
  assert.ok(partial.tracks.some(t=>t.frames>10000));
  evidence.scenarios.push({name:'navigation-preserves-partial',path:interrupted.path});
  console.log('PASS navigation preserves interrupted audio');
  // Media-element recovery is distinct from microphone/sender previews.
  await meeting.click('#join');
  await meeting.evaluate(async()=>{
    const context=new AudioContext();const osc=context.createOscillator();osc.frequency.value=770;
    const gain=context.createGain();gain.gain.value=.2;
    const destination=context.createMediaStreamDestination();osc.connect(gain).connect(destination);osc.start();await context.resume();
    const element=document.createElement('audio');element.muted=true;element.srcObject=destination.stream;document.body.append(element);await element.play();
    window.mediaTone={context,osc,element};
  });
  await meeting.bringToFront();
  execFileSync('xdotool',['search','--onlyvisible','--name','Cassini deterministic meeting','windowfocus','--sync','key','--clearmodifiers','ctrl+shift+9']);
  const mediaStart=await command('start','tracks');
  await poll(async()=> (await snapshot(mediaStart.path)).tracks.some(t=>t.audio.bytes.length>48000*2*1.6),'media capture');
  const mediaResult=await stop();const media=await snapshot(mediaResult.path,true);
  assert.equal(media.manifest.state,'completed');assert.equal(media.tracks.length,1);assert.equal(media.tracks[0].kind,'media');
  assert.ok(energy(media.tracks[0].audio,.3,.9,770).tone>.08,'media fallback retains synthetic audio streams');
  evidence.scenarios.push({name:'media-element-fallback',path:mediaResult.path});
  await meeting.evaluate(async()=>{mediaTone.element.remove();mediaTone.osc.stop();await mediaTone.context.close();});
  console.log('PASS media-element discovery fallback');
  // A Web Audio rendered tone exercises the independent mixed-tab fallback.
  await meeting.click('#join');
  await meeting.evaluate(async()=>{
    const context=new AudioContext();const osc=context.createOscillator();osc.frequency.value=990;
    const gain=context.createGain();gain.gain.value=.2;osc.connect(gain).connect(context.destination);osc.start();await context.resume();window.tabTone={context,osc};
  });
  await meeting.bringToFront();
  execFileSync('xdotool',['search','--onlyvisible','--name','Cassini deterministic meeting','windowfocus','--sync','key','--clearmodifiers','ctrl+shift+9']);
  const tabStart=await command('start','tab');
  await poll(async()=> (await snapshot(tabStart.path)).tracks.some(t=>t.audio.bytes.length>t.channels*48000*2*2.2),'tab capture');
  const tabResult=await stop();
  const tab=await snapshot(tabResult.path,true);
  assert.equal(tab.manifest.state,'completed');assert.equal(tab.tracks.length,1);assert.equal(tab.tracks[0].kind,'tab');
  assert.ok(energy(tab.tracks[0].audio,.5,1.5,990).tone>.08,'tab fallback contains rendered audio');
  evidence.scenarios.push({name:'tab-fallback',path:tabResult.path});
  console.log('PASS mixed tab fallback through real Chrome tabCapture');
  assert.deepEqual(evidence.errors,[]);
  await meeting.screenshot({path:path.join(artifacts,'meeting.png')});
  }
} catch(error) {
  evidence.failure=error.stack;
  evidence.diagnostics=[];
  for (const directory of sessions) {
    try {
      const captured=await snapshot(directory);
      evidence.diagnostics.push({directory,manifest:captured.manifest,energy:captured.tracks.map(t=>({id:t.id,
        rms:Array.from({length:Math.floor(t.audio.bytes.length/(2*t.channels*t.sampleRate)*10)},(_,i)=>energy(t.audio,i/10,(i+1)/10,440).rms)}))});
    } catch (diagnosticError) { evidence.diagnostics.push({directory,error:String(diagnosticError)}); }
  }
  throw error;
} finally {
  await writeFile(path.join(artifacts,'results.json'),JSON.stringify(evidence,null,2));
  await context?.close();server.close();await rm(profile,{recursive:true,force:true});
}
