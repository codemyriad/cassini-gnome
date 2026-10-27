// Full browser -> native control host -> broker -> GNOME widgets, and return path.
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {spawn,execFileSync} from 'node:child_process';
import {createServer} from 'node:http';
import {mkdtemp,mkdir,readFile,writeFile,chmod,rm} from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import {createInterface} from 'node:readline';
import {createWriteStream} from 'node:fs';
import {chromium} from 'playwright';
if(!process.env.CASSINI_ISOLATED_AUDIO || process.env.WAYLAND_DISPLAY) throw new Error('Use private recording harness');
const root=path.resolve(import.meta.dirname,'../..');
const artifacts=process.env.CASSINI_TEST_ARTIFACTS;
const extension=process.env.CASSINI_TEST_EXTENSION || path.join(root,'browser-extension');
const build=process.env.CASSINI_TEST_BUILD || path.join(root,'_build');
const profile=await mkdtemp(path.join(os.tmpdir(),'cassini-app-control-'));
const fixture=await readFile(path.join(root,'tests/recording/fixture.html'));
const server=createServer((req,res)=>{res.setHeader('Content-Type','text/html');res.end(fixture);});
await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
let context,remoteBrowser,ui;
const evidence={scenarios:[],console:[]};
const pending=[];
async function poll(check,label,timeout=20000) {
 const end=Date.now()+timeout;
 while(Date.now()<end) {const value=await check();if(value)return value;await new Promise(r=>setTimeout(r,100));}
 throw new Error(`Timed out: ${label}`);
}
function call(packet={action:'status'}) {
 return new Promise((resolve,reject)=>{
  const timer=setTimeout(()=>reject(new Error('GNOME UI request timed out')),12000);
  pending.push(value=>{clearTimeout(timer);value.error?reject(new Error(value.error)):resolve(value);});
  ui.stdin.write(JSON.stringify(packet)+'\n');
 });
}
async function hostManifest(name,script,id) {
 const file=path.join(profile,name+'.py');await writeFile(file,script);await chmod(file,0o700);
 await writeFile(path.join(profile,'NativeMessagingHosts',name+'.json'),JSON.stringify({name,description:'Isolated app control test',path:file,type:'stdio',allowed_origins:[`chrome-extension://${id}/`]}));
}
async function broker() {
 return JSON.parse(await new Promise((resolve,reject)=>{
  const p=spawn('/usr/bin/python3',['-c',`import sys,json;sys.path.insert(0,${JSON.stringify(root+'/src')});from cassini_viewer.browser_service import request;print(json.dumps(request()))`]);
  let out='';p.stdout.on('data',d=>out+=d);p.on('exit',code=>code?reject(new Error('Broker query failed')):resolve(out));
 }));
}
async function samples(directory) {
 const manifest=JSON.parse(await readFile(path.join(directory,'session.json')));
 const tracks=[];
 for(const track of manifest.tracks) {
  const bytes=await readFile(path.join(directory,track.file));
  tracks.push({...track,frames:(bytes.length-44)/(track.channels*2)});
 }
 return {manifest,tracks};
}
try {
 const id=[...createHash('sha256').update(extension).digest('hex').slice(0,32)].map(c=>String.fromCharCode(97+parseInt(c,16))).join('');
 await mkdir(path.join(profile,'NativeMessagingHosts'),{recursive:true});
 await hostManifest('com.codemyriad.cassini',`#!/usr/bin/python3\nimport sys\nsys.path.insert(0,${JSON.stringify(root+'/src')})\nfrom cassini_viewer.browser_host import main\nsys.exit(main(directory=${JSON.stringify(artifacts)},open_completed=False))\n`,id);
 await hostManifest('com.codemyriad.cassini.control',`#!/usr/bin/python3\nimport sys\nsys.path.insert(0,${JSON.stringify(root+'/src')})\nsys.stderr=open(${JSON.stringify(path.join(artifacts,"control-host.log"))},"a",buffering=1)\nfrom cassini_viewer.browser_control_host import main\nsys.exit(main())\n`,id);
 context=await chromium.launchPersistentContext(profile,{channel:'chromium',headless:false,args:[
  `--disable-extensions-except=${extension}`,`--load-extension=${extension}`,
  '--no-sandbox','--ozone-platform=x11','--autoplay-policy=no-user-gesture-required','--allow-loopback-in-peer-connection']});
 const worker=context.serviceWorkers()[0] || await context.waitForEvent('serviceworker');
 assert.equal(new URL(worker.url()).host,id,'unpacked extension ID matches prepared host allowlist');
 worker.on('console',message=>evidence.console.push(message.text()));
 ui=spawn('/usr/bin/python3',[path.join(root,'tests/recording/browser-ui.py'),'--build',build],{stdio:['pipe','pipe','pipe']});
 let stderr='';ui.stderr.on('data',data=>{stderr+=data;});
 ui.stderr.pipe(createWriteStream(path.join(artifacts,'gnome-ui.log')));
 createInterface({input:ui.stdout}).on('line',line=>{try{pending.shift()?.(JSON.parse(line));}catch{evidence.console.push(line);}});
 ui.on('exit',code=>{evidence.uiExit=code;});
 await poll(async()=> (await call()).status.includes('Listening inside your browser'),'control host connected');
 remoteBrowser=await chromium.launch({headless:false,args:['--no-sandbox','--ozone-platform=x11','--autoplay-policy=no-user-gesture-required','--allow-loopback-in-peer-connection']});
 const meeting=await context.newPage(),remote=await remoteBrowser.newPage();
 for(const page of [meeting,remote]) {await page.goto(`http://127.0.0.1:${server.address().port}/`);await page.click('#join');}
 await meeting.evaluate(async()=>{document.title='App-controlled synthetic meeting';await join(440);});
 await remote.evaluate(()=>join(880));
 const offer=await meeting.evaluate(async()=>{await meeting.pc.setLocalDescription(await meeting.pc.createOffer());return meeting.description();});
 const answer=await remote.evaluate(async offer=>{await meeting.pc.setRemoteDescription(offer);await meeting.pc.setLocalDescription(await meeting.pc.createAnswer());return meeting.description();},offer);
 await meeting.evaluate(answer=>meeting.pc.setRemoteDescription(answer),answer);
 const discovered=await poll(async()=>{const value=await call();return value.rows.find(row=>row.title==='App-controlled synthetic meeting') && value;},'meeting in GNOME list');
 const row=discovered.rows.find(row=>row.title==='App-controlled synthetic meeting');
 assert.equal(row.button,'Record');
 const distraction=await context.newPage();
 await distraction.goto(`http://127.0.0.1:${server.address().port}/?not-a-meeting`);
 await distraction.bringToFront(); // Record must target the selected row, never the active tab.
 await call({action:'record',id:row.id});
 await poll(async()=> (await call()).rows.some(r=>r.id===row.id && r.button==='Stop and save' && r.enabled),'recording indicator');
 let state;
 await poll(async()=>{
  state=await broker();
  const directory=state.statuses.find(s=>s.recording)?.path;
  return directory && (await samples(directory)).tracks.some(t=>t.frames>=t.sampleRate);
 },'one second recorded through app controls');
 const directory=state.statuses.find(s=>s.recording).path;
 execFileSync('xdotool',['search','--name','^Meetings in your browser$','windowraise']);
 execFileSync('import',['-window','root',path.join(artifacts,'app-recording.png')]);
 await call({action:'stop',id:row.id});
 await poll(async()=> (await call()).rows.some(r=>r.id===row.id && r.button==='Record' && r.enabled),'stopped GNOME indicator');
 const first=await samples(directory);
 assert.equal(first.manifest.state,'completed');
 assert.ok(first.tracks.some(t=>t.kind==='local') && first.tracks.some(t=>t.kind==='remote'));
 evidence.scenarios.push({name:'gnome-discover-explicit-row-record-stop',path:directory,tracks:first.manifest.tracks});
 await call({action:'auto',enabled:true});
 const automatic=await poll(async()=>{const value=await call();return Object.keys(value.notifications).length && value;},'automatic notification with Stop');
 const notification=Object.entries(automatic.notifications)[0];
 assert.ok(notification[1].actions.includes('stop'));
 await call({action:'close-window'});
 assert.equal((await call()).status,'Window closed');
 await call({action:'notification-stop',id:notification[0]});
 await poll(async()=> !Object.keys((await call()).notifications).length,'Stop works with meeting window closed');
 await meeting.evaluate(()=>meeting.pc.close());await remote.evaluate(()=>meeting.pc.close());
 await poll(async()=> !(await broker()).meetings.length,'first meeting ended while app window closed');
 await meeting.evaluate(()=>join(550));await remote.evaluate(()=>join(990));
 const nextOffer=await meeting.evaluate(async()=>{await meeting.pc.setLocalDescription(await meeting.pc.createOffer());return meeting.description();});
 const nextAnswer=await remote.evaluate(async offer=>{await meeting.pc.setRemoteDescription(offer);await meeting.pc.setLocalDescription(await meeting.pc.createAnswer());return meeting.description();},nextOffer);
 await meeting.evaluate(answer=>meeting.pc.setRemoteDescription(answer),nextAnswer);
 const nextAutomatic=await poll(async()=>{const value=await call();return Object.keys(value.notifications).length && value;},'new meeting automatically records while app window closed');
 await call({action:'notification-stop',id:Object.keys(nextAutomatic.notifications)[0]});
 await poll(async()=> !Object.keys((await call()).notifications).length,'new automatic recording stopped');
 await call({action:'reopen'});
 await poll(async()=> (await call()).rows.some(r=>r.button==='Record' && r.enabled),'reopened app reconciles stopped recording');
 await call({action:'auto',enabled:false});
 await meeting.evaluate(()=>meeting.pc.close());
 await poll(async()=> !(await call()).rows.length,'meeting removed after call ends');
 evidence.scenarios.push({name:'opt-in-auto-record-new-meeting-with-window-closed-notification-stop-teardown'});
 evidence.uiStderr=stderr;
 console.log('PASS real extension discovery -> GNOME list -> Record/Stop -> saved source WAVs; auto-record notification Stop and teardown');
} catch(error) {evidence.failure=error.stack;throw error;}
finally {
 if(ui && ui.exitCode===null) {
  try {await call({action:'quit'});ui.stdin.end();await Promise.race([new Promise(resolve=>ui.once('exit',resolve)),new Promise(resolve=>setTimeout(resolve,2000))]);} catch {}
  if(ui.exitCode===null) ui.kill();
 }
 await context?.close();await remoteBrowser?.close();server.close();
 await writeFile(path.join(artifacts,'app-control-results.json'),JSON.stringify(evidence,null,2));
 await rm(profile,{recursive:true,force:true});
}
