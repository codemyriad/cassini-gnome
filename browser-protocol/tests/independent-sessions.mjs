// SPDX-License-Identifier: GPL-3.0-or-later
// Loaded extension, real WebRTC and native framing; private fixture control broker.
// CASSINI_NATIVE_HOST/CASSINI_CONTROL_NATIVE_HOST can select the product adapters.
import assert from 'node:assert/strict';
import {createServer as httpServer} from 'node:http';
import {createServer as unixServer} from 'node:net';
import {execFileSync} from 'node:child_process';
import {mkdtemp,mkdir,readFile,writeFile,chmod,rm} from 'node:fs/promises';
import path from 'node:path';import os from 'node:os';import {fileURLToPath} from 'node:url';
import {connectReceiveSlots} from '../scenarios/product-topologies.mjs';
const {chromium}=await import(process.env.PLAYWRIGHT_MODULE||'playwright');
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const extension=path.resolve(process.env.CASSINI_TEST_EXTENSION||path.join(root,'extension'));
const id='pcafdaahfhjiolfpfnlhmljfgoabbaij';
if(process.env.CASSINI_ISOLATED_AUDIO!=='1')throw Error('Use the private audio harness, never the desktop sound server.');
const pulse=process.env.PULSE_SERVER||'';
if(!/^unix:\/tmp\/cassini-audio-[^/]+\/pulse\.sock$/.test(pulse)&&!(process.env.CI==='true'&&/^unix:\/run\/user\/\d+\/pulse\/native$/.test(pulse)))throw Error('A private Pulse socket is required.');
function pactl(...args){return execFileSync('pactl',['--server='+pulse,...args],{encoding:'utf8'}).trim();}
const sink=pactl('get-default-sink');
assert.ok(['cassini_desktop','cassini_ci'].includes(sink));
const temporary=await mkdtemp(path.join(os.tmpdir(),'cassini-independent-'));
const profile=path.join(temporary,'profile'),audioRoot=path.join(temporary,'audio');
await mkdir(audioRoot);await mkdir(profile);
const socketPath=path.join(temporary,'control.sock'),events=[];
let control,context,remoteBrowser;
const server=unixServer(connection=>{control=connection;let pending='';connection.setEncoding('utf8');connection.on('data',data=>{pending+=data;for(let end;(end=pending.indexOf('\n'))>=0;){events.push(JSON.parse(pending.slice(0,end)));pending=pending.slice(end+1);}});});
await new Promise(resolve=>server.listen(socketPath,resolve));
const web=httpServer((_req,response)=>response.end('<!doctype html><title>Cassini isolated source fixture</title><button>Join</button>'));
await new Promise(resolve=>web.listen(0,'127.0.0.1',resolve));
const address=`http://127.0.0.1:${web.address().port}/`;
async function until(check,label,timeout=20000){const end=Date.now()+timeout;while(Date.now()<end){const value=await check();if(value)return value;await new Promise(r=>setTimeout(r,25));}throw Error('Timed out: '+label);}
let sequence=0;
async function command(action,tabId,extra={},timeout=20000){const requestId='independent-'+(++sequence);control.write(JSON.stringify({type:'command',requestId,action,tabId,mode:'tracks',independent:true,captureLocal:false,microphone:false,...extra})+'\n');const result=await until(()=>events.find(e=>e.type==='result'&&e.requestId===requestId),requestId,timeout);assert.equal(result.ok,true,JSON.stringify(result));return result.status;}
async function manifest(directory){return JSON.parse(await readFile(path.join(directory,'session.json')));}
async function count(directory){try {return (await manifest(directory)).tracks.reduce((n,t)=>n+t.frames,0);}catch{return 0;}}
function power(data,frequency,rate=48000){const n=12000,start=data.length/2-n;assert.ok(start>=0);let re=0,im=0;for(let i=0;i<n;i++){const sample=data.readInt16LE((start+i)*2)/32768;re+=sample*Math.cos(2*Math.PI*frequency*i/rate);im+=sample*Math.sin(2*Math.PI*frequency*i/rate);}return(re*re+im*im)/(n*n);}
const report={sink,pulse:'private',cases:[]};
const meetings=[];
try {
  const hosts=path.join(profile,'NativeMessagingHosts');await mkdir(hosts);
  for(const [name,fixture,configured] of [
    ['io.codemyriad.cassini','browser_host_fixture.py',process.env.CASSINI_NATIVE_HOST],
    ['io.codemyriad.cassini.control','control_host_fixture.py',process.env.CASSINI_CONTROL_NATIVE_HOST],
  ]){
    const host=path.join(temporary,name+'.py');
    const source=path.join(root,'tests',fixture);
    const environment=`import os\nos.environ['CASSINI_LIBRARY_DIR']=${JSON.stringify(audioRoot)}\n`;
    const program=environment+(configured
      ? `import os\nos.environ['CASSINI_CONTROL_SOCKET']=${JSON.stringify(socketPath)}\nos.execv(${JSON.stringify(path.resolve(configured))},[${JSON.stringify(path.resolve(configured))}])\n`
      : `import os,sys\nos.environ['CASSINI_CONTROL_SOCKET']=${JSON.stringify(socketPath)}\nsys.argv=[${JSON.stringify(source)},${JSON.stringify(audioRoot)}]\nexec(compile(open(sys.argv[0]).read(),sys.argv[0],'exec'))\n`);
    await writeFile(host,'#!/usr/bin/env python3\n'+program);await chmod(host,0o700);
    await writeFile(path.join(hosts,name+'.json'),JSON.stringify({name,description:'Cassini private concurrency fixture',path:host,type:'stdio',allowed_origins:[`chrome-extension://${id}/`]}));
  }
  context=await chromium.launchPersistentContext(profile,{channel:'chromium',headless:true,ignoreDefaultArgs:['--mute-audio'],args:[`--disable-extensions-except=${extension}`,`--load-extension=${extension}`,`--allowlisted-extension-id=${id}`,'--autoplay-policy=no-user-gesture-required','--allow-loopback-in-peer-connection','--disable-features=WebRtcHideLocalIpsWithMdns']});
  const worker=context.serviceWorkers()[0]||await context.waitForEvent('serviceworker');
  assert.equal(new URL(worker.url()).host,id);
  remoteBrowser=await chromium.launch({headless:true,args:['--autoplay-policy=no-user-gesture-required','--allow-loopback-in-peer-connection','--disable-features=WebRtcHideLocalIpsWithMdns']});
  const remoteContext=await remoteBrowser.newContext();
  for(const [index,frequency] of [440,880].entries()){
    const page=await context.newPage(),remote=await remoteContext.newPage();await page.goto(address);await remote.goto(address);await page.click('button');
    await page.evaluate(index=>document.title='Independent meeting '+(index+1),index);
    await connectReceiveSlots(page,remote,[[frequency]]);
    // Two rendered aliases of one receiver exercise clone dedup. Muting tab
    // and sink below must not stop this source's recording graph.
    await page.evaluate(async()=>{const track=topologyPeer.getReceivers()[0].track;for(const value of [track,track.clone()]){const audio=document.createElement('audio');audio.srcObject=new MediaStream([value]);audio.autoplay=true;document.body.append(audio);await audio.play();}});
    meetings.push({page,frequency});
  }
  await until(()=>events.find(e=>e.type==='hello'&&e.capabilities?.includes('independent-sessions-v1')),'capability handshake');
  const inventory=await until(()=>events.filter(e=>e.type==='meetings').at(-1)?.meetings.filter(m=>m.title.startsWith('Independent meeting')&&m.sourceCount>0).length===2&&events.filter(e=>e.type==='meetings').at(-1),'two active calls');
  for(const [index,meeting] of meetings.entries())meeting.tabId=inventory.meetings.find(m=>m.title==='Independent meeting '+(index+1)).tabId;
  pactl('set-sink-mute',sink,'1');assert.match(pactl('get-sink-mute',sink),/yes/);
  for(const meeting of meetings)await worker.evaluate(tabId=>chrome.tabs.update(tabId,{muted:true}),meeting.tabId);
  assert.ok(await worker.evaluate(async tabs=>(await Promise.all(tabs.map(id=>chrome.tabs.get(id)))).every(tab=>tab.mutedInfo.muted),meetings.map(m=>m.tabId)));
  // Block the actual management start for one tab, not merely the exported
  // background start() helper used by the old VM reservation test. Another
  // tab's command must complete without releasing this lookup.
  await worker.evaluate(heldTab => {
    const get = chrome.tabs.get.bind(chrome.tabs);
    let release; const gate = new Promise(resolve => release = resolve);
    globalThis.cassiniHeldLookupEntered = false;
    globalThis.cassiniReleaseLookup = () => { chrome.tabs.get = get; release(); };
    chrome.tabs.get = async id => {
      if (id === heldTab) { globalThis.cassiniHeldLookupEntered = true; await gate; }
      return get(id);
    };
  }, meetings[0].tabId);
  const heldStart = command('start',meetings[0].tabId);
  heldStart.catch(() => {}); // Preserve the original failure if a later assertion fails.
  await until(() => worker.evaluate(() => globalThis.cassiniHeldLookupEntered),'held management lookup');
  try {
    const second = await command('start',meetings[1].tabId,{},5000);
    meetings[1].directory = second.path;
    assert.equal(await worker.evaluate(() => globalThis.cassiniHeldLookupEntered),true);
    report.pendingLookupIsolation = true;
  } finally { await worker.evaluate(() => globalThis.cassiniReleaseLookup()); }
  meetings[0].directory = (await heldStart).path;
  for(const meeting of meetings)assert.ok(path.resolve(meeting.directory).startsWith(audioRoot+path.sep),'test capture must stay inside its private Library');
  assert.notEqual(meetings[0].directory,meetings[1].directory,'independent native host creates distinct folders');
  await Promise.all(meetings.map(m=>until(async()=>await count(m.directory)>=96000,'complete samples while muted')));
  for(const meeting of meetings){const value=await manifest(meeting.directory);assert.equal(value.tracks.length,1,'receiver and its media clone share one source');const track=value.tracks[0];assert.equal(track.kind,'remote');const raw=(await readFile(path.join(meeting.directory,track.file))).subarray(44);const target=power(raw,meeting.frequency),other=power(raw,meeting.frequency===440?880:440);assert.ok(target>0.001,'muted source tone is present');assert.ok(other<target*0.01,'no bleed between independent meetings');report.cases.push({frequency:meeting.frequency,frames:track.frames,tonePower:target,otherPower:other,sinkMuted:true,tabMuted:true});}
  const stopped=await command('stop',meetings[0].tabId);assert.equal(stopped.sessions.length,1);assert.equal(stopped.sessions[0].tabId,meetings[1].tabId);
  const firstFrames=await count(meetings[0].directory),secondFrames=await count(meetings[1].directory);
  await until(async()=>await count(meetings[1].directory)>=secondFrames+48000,'second meeting continues after first Stop');
  assert.equal(await count(meetings[0].directory),firstFrames);
  await command('stop',meetings[1].tabId);
  assert.equal((await manifest(meetings[0].directory)).state,'completed');assert.equal((await manifest(meetings[1].directory)).state,'completed');
  report.independentStop=true;
  await mkdir(path.join(root,'artifacts'),{recursive:true});await writeFile(path.join(root,'artifacts/independent-sessions.json'),JSON.stringify(report,null,2)+'\n');
  console.log('PASS two independent native sessions: private sink and both tabs muted, isolated tones, clone dedup, individual Stop preserves other capture');
} catch(error) {
  report.error=String(error.stack||error);report.events=events.slice(-100);
  report.manifests=[];
  for(const meeting of meetings)if(meeting.directory){try{report.manifests.push(await manifest(meeting.directory));}catch(failure){report.manifests.push({error:String(failure)});}}
  await mkdir(path.join(root,'artifacts'),{recursive:true});await writeFile(path.join(root,'artifacts/independent-failure.json'),JSON.stringify(report,null,2)+'\n');
  throw error;
} finally {
  pactl('set-sink-mute',sink,'0');await context?.close();await remoteBrowser?.close();control?.destroy();server.close();web.close();await rm(temporary,{recursive:true,force:true});
}
