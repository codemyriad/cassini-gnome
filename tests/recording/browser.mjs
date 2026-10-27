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
if (!process.env.CASSINI_ISOLATED_AUDIO || process.env.WAYLAND_DISPLAY) throw new Error('Run through tests/recording/run.sh to isolate desktop and audio.');
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const artifacts = path.resolve(process.env.CASSINI_TEST_ARTIFACTS || path.join(root, 'tests/recording/artifacts/browser'));
await mkdir(artifacts, {recursive:true});
const profile = await mkdtemp(path.join(os.tmpdir(), 'cassini-chromium-'));
const fixture = await readFile(path.join(root, 'tests/recording/fixture.html'));
const server = createServer((req,res) => {res.setHeader('Content-Type','text/html');res.end(fixture);});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
let context;
const evidence = {scenarios: [], errors: []};
function wav(buffer) {
  assert.equal(buffer.toString('ascii', 0, 4), 'RIFF');
  assert.equal(buffer.readUInt32LE(40), buffer.length - 44);
  return {rate:buffer.readUInt32LE(24), channels:buffer.readUInt16LE(22), bytes:buffer.subarray(44)};
}
function energy(audio, start, end, hz) {
  const first = Math.max(0,Math.round(start * audio.rate));
  const last = Math.min(audio.bytes.length / (2*audio.channels), Math.round(end * audio.rate));
  let re=0,im=0,power=0;
  for(let n=first;n<last;n++) {
    const v=audio.bytes.readInt16LE(n*2*audio.channels)/32768;
    power+=v*v; re+=v*Math.cos(2*Math.PI*hz*n/audio.rate); im+=v*Math.sin(2*Math.PI*hz*n/audio.rate);
  }
  return {rms:Math.sqrt(power/Math.max(1,last-first)), tone:2*Math.hypot(re,im)/Math.max(1,last-first)};
}
async function snapshot(directory) {
  const manifest=JSON.parse(await readFile(path.join(directory,'session.json')));
  const tracks=[];
  for(const track of manifest.tracks) tracks.push({...track,audio:wav(await readFile(path.join(directory,track.file)))});
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
  const id=new URL(worker.url()).host;
  const shim=path.join(profile,'host.py');
  await writeFile(shim,`#!/usr/bin/python3\nimport importlib.util\nspec=importlib.util.spec_from_file_location('host',${JSON.stringify(path.join(root,'browser-extension/native_host.py'))})\nmodule=importlib.util.module_from_spec(spec)\nspec.loader.exec_module(module)\nraise SystemExit(module.main(directory=${JSON.stringify(artifacts)},open_completed=False))\n`);
  await chmod(shim,0o700);
  await mkdir(path.join(profile,'NativeMessagingHosts'), {recursive:true});
  await writeFile(path.join(profile,'NativeMessagingHosts/com.codemyriad.cassini.json'),JSON.stringify({
    name:'com.codemyriad.cassini',description:'Cassini integration test',path:shim,type:'stdio',allowed_origins:[`chrome-extension://${id}/`]
  }));
  if (process.env.CASSINI_TALK_ONLY) {
    await (await import('./talk.mjs')).run({context,worker,id,artifacts});
  } else {
  const meeting=await context.newPage(), remote=await context.newPage();
  for(const page of [meeting,remote]) {
    page.on('pageerror',e=>evidence.errors.push(e.message));
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
  await new Promise(resolve=>setTimeout(resolve,500));
  assert.ok((await worker.evaluate(()=>chrome.runtime.getContexts({}))).some(c=>c.contextType==='POPUP'),'real extension action was invoked');
  const popup=await context.newPage();
  await popup.goto(`chrome-extension://${id}/popup.html`);
  await meeting.bringToFront();
  async function command(type, mode) {
    const result=await popup.evaluate(({type,mode})=>chrome.runtime.sendMessage({target:'background',type,mode}),{type,mode});
    if(result.error) {console.error(await worker.evaluate(()=>chrome.tabs.query({active:true,currentWindow:true})));throw new Error(result.error);}
    return result;
  }
  async function stop() {
    await command('stop');
    let result;
    for(let n=0;n<100;n++) {
      result=await command('status');
      if(!result.recording) return result;
      await new Promise(resolve=>setTimeout(resolve,100));
    }
    throw new Error('Stop timed out');
  }
  const started=await command('start','tracks');
  assert.equal(started.recording,true);
  let initialLocal, initialManifest;
  for(let n=0;n<100;n++) {
    initialManifest=JSON.parse(await readFile(path.join(started.path,'session.json')));
    initialLocal=initialManifest.tracks.find(t=>t.kind==='local');
    if(initialLocal) {
      const bytes=await readFile(path.join(started.path,initialLocal.file));
      if(bytes.length>44+2*48000*2) break;
    }
    await meeting.waitForTimeout(100);
  }
  assert.ok(initialLocal,'local audio source registered');
  const beforeMute=await readFile(path.join(started.path,initialLocal.file));
  const muteFrame=(beforeMute.length-44)/(initialLocal.channels*2);
  assert.ok(muteFrame>=48000*2,'two seconds actually captured before muting');
  let state=await command('status');
  assert.equal(state.tracks,2,JSON.stringify(state));
  const muteEpoch=await meeting.evaluate(()=>{meeting.mute(true);return Date.now();});
  await meeting.waitForTimeout(800);
  await meeting.evaluate(()=>meeting.mute(false));
  await meeting.waitForTimeout(700);
  await meeting.evaluate(()=>meeting.replace(660));
  await meeting.waitForTimeout(1800);
  await meeting.evaluate(()=>{
    const preview=document.createElement('audio');preview.muted=true;
    preview.srcObject=new MediaStream([meeting.sender.track]);document.body.append(preview);
    meeting.pc.removeTrack(meeting.sender);
  });
  await meeting.waitForTimeout(700);
  assert.equal((await command('status')).tracks,3,'removed local preview must not become a generic media recording');
  const stopped=await stop();
  const first=await snapshot(stopped.path);
  assert.equal(first.manifest.state,'completed',JSON.stringify(first.manifest));
  assert.equal(first.tracks.length,3,'replacement creates a distinct source, not a participant identity');
  const local=first.tracks.filter(t=>t.kind==='local'), received=first.tracks.find(t=>t.kind==='remote');
  assert.equal(local.length,2); assert.ok(received);
  assert.ok(energy(local[0].audio,0.5,1.5,440).tone > .1,'local 440 Hz source');
  assert.ok(energy(local[0].audio,0.5,1.5,880).tone < .01,'no remote bleed into local source');
  assert.ok(energy(received.audio,0.5,1.5,880).tone > .08,'remote 880 Hz source');
  assert.ok(energy(received.audio,0.5,1.5,440).tone < .01,'no local bleed into remote source');
  const mutePosition=muteFrame/48000;
  const wallMutePosition=(muteEpoch-Date.parse(first.manifest.createdAt))/1000;
  assert.ok(Math.abs(mutePosition+local[0].startOffsetMs/1000-wallMutePosition)<.4,
    `render timeline follows real elapsed time: samples=${mutePosition}, offset=${local[0].startOffsetMs}, wall=${wallMutePosition}`);
  assert.ok(energy(local[0].audio,mutePosition+.2,mutePosition+.5,440).rms < .005,'meeting mute becomes silence after the measured sample position');
  assert.ok(energy(local[1].audio,.3,.9,660).tone > .1,'replacement source contains new tone');
  assert.ok(local[1].startOffsetMs > 3000,'replacement is positioned on the shared session timeline');
  evidence.scenarios.push({name:'webrtc-separation-mute-replace-stop',path:stopped.path,tracks:first.manifest.tracks});
  console.log('PASS real WebRTC source separation, mute, replacement, timeline and stop');
  // Repeated start, then navigation: retain playable partial files with an explicit interruption state.
  await command('start','tracks');
  await meeting.waitForTimeout(1000);
  await meeting.goto(`http://127.0.0.1:${server.address().port}/?navigated`);
  let interrupted;
  for(let i=0;i<100;i++) {interrupted=await command('status');if(!interrupted.recording)break;await meeting.waitForTimeout(100);}
  assert.equal(interrupted.recording,false);
  const partial=await snapshot(interrupted.path);
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
  await meeting.waitForTimeout(300);
  await command('start','tracks');await meeting.waitForTimeout(1600);
  const mediaResult=await stop();const media=await snapshot(mediaResult.path);
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
  await meeting.waitForTimeout(300);
  await command('start','tab');
  await meeting.waitForTimeout(2200);
  const tabResult=await stop();
  const tab=await snapshot(tabResult.path);
  assert.equal(tab.manifest.state,'completed');assert.equal(tab.tracks.length,1);assert.equal(tab.tracks[0].kind,'tab');
  assert.ok(energy(tab.tracks[0].audio,.5,1.5,990).tone>.08,'tab fallback contains rendered audio');
  evidence.scenarios.push({name:'tab-fallback',path:tabResult.path});
  console.log('PASS mixed tab fallback through real Chrome tabCapture');
  assert.deepEqual(evidence.errors,[]);
  await meeting.screenshot({path:path.join(artifacts,'meeting.png')});
  }
} catch(error) {
  evidence.failure=error.stack;
  throw error;
} finally {
  await writeFile(path.join(artifacts,'results.json'),JSON.stringify(evidence,null,2));
  await context?.close();server.close();await rm(profile,{recursive:true,force:true});
}
