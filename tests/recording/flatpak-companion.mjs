// Actual installed Flatpak relays + loaded extension, on private X11/Pulse only.
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
import {createServer} from 'node:http';
import {mkdir, readFile, writeFile, symlink, cp} from 'node:fs/promises';
import path from 'node:path';
import {chromium} from 'playwright';
assert.equal(process.env.CASSINI_ISOLATED_AUDIO, '1', 'Use tests/recording/run.sh');
assert.ok(!process.env.WAYLAND_DISPLAY);
assert.match(process.env.PULSE_SERVER || '', /^unix:\/tmp\/cassini-audio-/);
const application = process.env.CASSINI_TEST_FLATPAK_ID || 'io.codemyriad.CassiniTalk.Devel';
const root = path.resolve(import.meta.dirname, '../..'), evidence = {scenarios: [], console: []};
const artifacts = process.env.CASSINI_TEST_ARTIFACTS;
const config = process.env.XDG_CONFIG_HOME, home = process.env.HOME;
assert.match(home, /^\/tmp\/cassini-audio-/);
assert.equal(process.env.CASSINI_LIBRARY_DIR, undefined, 'Unset the Library override before starting the private bus');
await mkdir(config, {recursive:true});
await mkdir(path.join(home, 'Music'), {recursive:true});
await writeFile(path.join(config, 'user-dirs.dirs'), `XDG_MUSIC_DIR="${home}/Music"\n`);
const tools = path.join(home, 'bin'); await mkdir(tools);
await symlink(chromium.executablePath(), path.join(tools, 'chromium'));
process.env.PATH = tools + ':' + process.env.PATH;
// This is the harness's private bus. The host helper must inherit its private
// browser inventory and the selected private installation, before activation.
execFileSync('dbus-update-activation-environment', ['PATH', 'HOME', 'XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_CACHE_HOME', 'FLATPAK_USER_DIR', 'PULSE_SERVER']);
const base = ['run', '--user', '--no-documents-portal', '--command=python3', application, '-I', '-c'];
function inside(code) {
 return JSON.parse(execFileSync('flatpak', [...base, "import sys;sys.path.insert(0,'/app/share/cassini-talk');" + code], {encoding:'utf8', timeout:20000}));
}
function rpc(action='status', values={}) {
 return inside(`import json;from cassini_talk.browser_service import request,ensure_service;print(json.dumps(request(${JSON.stringify(action)},path=ensure_service(),**json.loads(${JSON.stringify(JSON.stringify(values))}))))`);
}
const location = execFileSync('flatpak', ['info','--user','--show-location',application], {encoding:'utf8'}).trim();
const extension = path.join(location,'files/share/cassini-talk/browser-extension');
const installed = inside("import json;from cassini_talk import companion;assert companion.available();print(json.dumps([str(p) for p in companion.register()]))");
assert.ok(installed.includes(path.join(config,'chromium/NativeMessagingHosts/io.codemyriad.cassini.json')));
const fixture = await readFile(path.join(root,'tests/recording/fixture.html'));
const server = createServer((_q,res) => {res.setHeader('Content-Type','text/html');res.end(fixture);});
await new Promise(resolve => server.listen(0,'127.0.0.1',resolve));
async function poll(check, label, timeout=20000) {
 const end=Date.now()+timeout;
 while(Date.now()<end) {const value=await check();if(value)return value;await new Promise(r=>setTimeout(r,100));}
 throw new Error('Timed out: '+label);
}
async function samples(directory) {
 const manifest=JSON.parse(await readFile(path.join(directory,'session.json'))), tracks=[];
 for(const track of manifest.tracks) {
  const data=await readFile(path.join(directory,track.file));
  tracks.push({...track,frames:(data.length-44)/(2*track.channels),data});
 }
 return {manifest,tracks};
}
function power(track, hz) {
 const count=Math.min(track.frames,48000), start=track.frames-count;
 let re=0,im=0;
 for(let i=0;i<count;i++) {const sample=track.data.readInt16LE(44+(start+i)*track.channels*2)/32768,a=2*Math.PI*hz*i/track.sampleRate;re+=sample*Math.cos(a);im+=sample*Math.sin(a);}
 return (re*re+im*im)/(count*count);
}
let context, remote, directories=[];
try {
 const flags=['--no-sandbox','--ozone-platform=x11','--autoplay-policy=no-user-gesture-required','--allow-loopback-in-peer-connection','--disable-features=WebRtcHideLocalIpsWithMdns'];
 context=await chromium.launchPersistentContext(path.join(config,'chromium'), {channel:'chromium',headless:false,ignoreDefaultArgs:['--mute-audio'],args:[`--load-extension=${extension}`,`--disable-extensions-except=${extension}`,...flags]});
 const worker=context.serviceWorkers()[0] || await context.waitForEvent('serviceworker');
 const id=new URL(worker.url()).host;assert.equal(id,'pcafdaahfhjiolfpfnlhmljfgoabbaij');
 worker.on('console', message=>evidence.console.push(message.text()));
 await poll(()=>rpc().connectedBrowsers===1,'installed host handshake');
 assert.ok(rpc().browserNames.includes('Chrome'), 'host-side browser identity crosses sandbox');
 remote=await chromium.launch({headless:false,ignoreDefaultArgs:['--mute-audio'],args:flags});
 const pages=[];
 for(let i=0;i<2;i++) {
  const meeting=await context.newPage(), other=await remote.newPage();pages.push(meeting);
  for(const page of [meeting,other]) {await page.goto(`http://127.0.0.1:${server.address().port}/`);await page.click('#join');}
  await meeting.evaluate(async i=>{document.title=`Flatpak call ${i}`;await window.join(440+i*110);},i);
  await other.evaluate(hz=>window.join(hz),[880,990][i]);
  const offer=await meeting.evaluate(async()=>{await meeting.pc.setLocalDescription(await meeting.pc.createOffer());return meeting.description();});
  const answer=await other.evaluate(async offer=>{await meeting.pc.setRemoteDescription(offer);await meeting.pc.setLocalDescription(await meeting.pc.createAnswer());return meeting.description();},offer);
  await meeting.evaluate(answer=>meeting.pc.setRemoteDescription(answer),answer);
  await poll(()=>rpc().meetings.some(m=>m.title===`Flatpak call ${i}`),'browser call discovery');
  await worker.evaluate(async title=>{const tab=(await chrome.tabs.query({})).find(t=>t.title===title);await chrome.tabs.update(tab.id,{muted:true});},`Flatpak call ${i}`);
 }
 execFileSync('pactl',['--server',process.env.PULSE_SERVER,'set-sink-mute','cassini_desktop','1']);
 rpc('start-all',{microphone:false});
 const states=await poll(()=>{const states=rpc().statuses.filter(s=>s.recording&&s.path);return states.length===2&&states;},'two installed Flatpak captures');
 directories=states.map(s=>s.path);assert.notEqual(...directories);
 for(let i=0;i<2;i++) {
  const audio=await poll(async()=>{const audio=await samples(directories[i]);return audio.tracks.some(t=>t.kind==='remote'&&t.frames>=96000)&&audio;},'two seconds of source PCM');
  assert.equal(audio.tracks.filter(t=>t.kind==='microphone'||t.kind==='local').length,0,'per-session mic off');
  const track=audio.tracks.find(t=>t.kind==='remote'), frequency=states[i].title.endsWith('0')?880:990;
  const wanted=power(track,frequency), bleed=power(track,frequency===880?990:880);
  assert.ok(wanted>.001&&bleed<wanted*.01,JSON.stringify({wanted,bleed}));
  evidence.scenarios.push({name:'installed-muted-source',directory:directories[i],wanted,bleed});
 }
 // Worker death must not kill the broker's own sandbox or duplicate meetings.
 const cdp=await context.newCDPSession(pages[0]);await cdp.send('ServiceWorker.enable');
 await cdp.send('ServiceWorker.stopAllWorkers');
 await poll(async()=>{for(const directory of directories) if(!(await samples(directory)).manifest.events.some(e=>e.type==='othersSource'&&e.source==='system'&&e.reason==='lost'))return false;return true;},'system fallback survives worker exit');
 await cdp.detach();await pages[0].reload();
 await poll(()=>rpc().connectedBrowsers===1,'fresh content wakes actual companion');
 const restored=rpc().statuses.filter(s=>s.recording&&s.path);
 assert.deepEqual(restored.map(s=>s.path).sort(),[...directories].sort());
 rpc('stop',{id:restored[0].id});
 await poll(()=>rpc().statuses.filter(s=>s.recording).length===1,'individual Stop through installed relay');
 rpc('stop-all');await poll(()=>!rpc().statuses.some(s=>s.recording),'global Stop');
 for(const directory of directories) assert.equal((await samples(directory)).manifest.state,'completed');
 evidence.scenarios.push({name:'worker-loss-fallback-reconnect-same-folders-individual-global-stop',directories});
 console.log('PASS installed Flatpak companion: concurrent muted sources, per-session mic off, worker loss/fallback/reconnect and Stop');
} catch(error) {evidence.failure=error.stack;try {evidence.status=rpc();} catch {} throw error;}
finally {
 try {rpc('stop-all');} catch {}
 for(const directory of directories) {
  try {await cp(directory,path.join(artifacts,'library',path.basename(directory)),{recursive:true});}
  catch(error) {(evidence.copyErrors??=[]).push(error.message);}
 }
 await context?.close();await remote?.close();server.close();
 try {inside("import shutil,json;from cassini_talk.browser_service import service_path;shutil.rmtree(service_path().parent,ignore_errors=True);print(json.dumps(True))");}
 catch(error) {evidence.cleanupError=error.message;}
 await writeFile(path.join(artifacts,'flatpak-companion.json'),JSON.stringify(evidence,null,2));
}
