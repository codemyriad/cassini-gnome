// D39 fresh-profile installation entirely through the actual app and Chromium UI.
import assert from 'node:assert/strict';
import {spawn, execFileSync} from 'node:child_process';
import {createHash} from 'node:crypto';
import {cp, mkdir, mkdtemp, readFile, writeFile, chmod, access} from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import {createInterface} from 'node:readline';
import {createWriteStream} from 'node:fs';
import {chromium} from 'playwright';
assert.equal(process.env.CASSINI_ISOLATED_AUDIO, '1');
assert.ok(!process.env.WAYLAND_DISPLAY);
const root = path.resolve(import.meta.dirname, '../..');
const out = process.env.CASSINI_TEST_ARTIFACTS;
const build = process.env.CASSINI_TEST_BUILD;
const profile = path.join(process.env.XDG_CONFIG_HOME, 'chromium');
const resource = await mkdtemp(path.join(os.tmpdir(), 'cassini-companion-bundle-'));
await cp(path.join(root, 'browser-extension'), resource, {recursive:true});
const bin = await mkdtemp(path.join(os.tmpdir(), 'cassini-companion-bin-'));
// The app chooses a real supported browser. The wrapper only binds Chromium
// to this test's private profile; it never loads or installs an extension.
const wrapper = path.join(bin, 'chromium');
const quote = s => "'" + s.replaceAll("'", "'\\''") + "'";
await writeFile(wrapper, `#!/bin/sh
printf '%s\\n' "$@" >> ${quote(path.join(out,'browser-launch-args.txt'))}
exec ${quote(chromium.executablePath())} --no-sandbox --user-data-dir=${quote(profile)} "$@"
`);
await chmod(wrapper, 0o700);
const environment = {...process.env, PATH: bin+':'+process.env.PATH, CASSINI_LIBRARY_DIR:path.join(out,'Library')};
const evidence = {method:'actual Developer mode and Load unpacked UI', expectedID:'pcafdaahfhjiolfpfnlhmljfgoabbaij', storeRequests:[], stages:[]};
let app, context;
const pending=[];
function launchApp(label) {
 app = spawn('/usr/bin/python3',[path.join(root,'tests/recording/companion-ui.py'),'--build',build,'--bundle',resource],
             {stdio:['pipe','pipe','pipe'],env:environment});
 app.stderr.pipe(createWriteStream(path.join(out,label+'.log')));
 createInterface({input:app.stdout}).on('line',line=>{
  try {const value=JSON.parse(line); pending.shift()?.(value);} catch {evidence.stages.push({output:line});}
 });
}
function call(action='state',values={}) {
 return new Promise((resolve,reject)=>{
  const timer=setTimeout(()=>reject(new Error('App UI timed out: '+action)),12000);
  pending.push(value=>{clearTimeout(timer); value.error ? reject(new Error(value.error)) : resolve(value);});
  app.stdin.write(JSON.stringify({action,...values})+'\n');
 });
}
async function poll(check,label,timeout=20000) {
 const end=Date.now()+timeout;
 while(Date.now()<end) {const result=await check(); if(result)return result; await new Promise(r=>setTimeout(r,100));}
 throw new Error('Timed out: '+label);
}
async function launchBrowser() {
 context = await chromium.launchPersistentContext(profile,{headless:false,ignoreDefaultArgs:['--disable-extensions'],
  args:['--no-sandbox','--force-renderer-accessibility'],viewport:{width:1100,height:720},env:environment});
 context.on('request',request=>{if(request.url().includes('chromewebstore.google.com')) evidence.storeRequests.push(request.url());});
 await context.route('https://chromewebstore.google.com/**',route=>route.abort());
 return context.pages()[0];
}
async function stopApp() {
 const exited=new Promise(resolve=>app.once('exit',resolve));
 await call('quit');
 await Promise.race([exited,new Promise((_,reject)=>setTimeout(()=>reject(new Error('App did not quit')),12000))]);
}
try {
 await mkdir(path.join(out,'Library'),{recursive:true});
 launchApp('app-first');
 await poll(async()=>['waiting','connected'].includes((await call()).phase),'background host registration');
 assert.equal((await call()).connected,false,'a copied folder is never an added/connected companion');
 await launchBrowser();
 await call('add');
 const state=await poll(async()=>{const state=await call();return state.guide&&state.copyReady ? state : null;},'Add from Cassini guide');
 assert.equal(state.primary,'Add from Cassini');
 assert.equal(state.done,false);
 const installed=JSON.parse(await readFile(path.join(state.folder,'manifest.json')));
 const id=[...createHash('sha256').update(Buffer.from(installed.key,'base64')).digest('hex').slice(0,32)].map(c=>String.fromCharCode(97+parseInt(c,16))).join('');
 assert.equal(id,evidence.expectedID);
 await call('shot',{path:path.join(out,'companion-local-guide.png')});
 await call('copy');
 await call('open');
 const page=await poll(()=>context.pages().find(page=>page.url().startsWith('chrome://extensions')),'app opens Extensions page');
 await page.waitForSelector('extensions-manager');
 const manager=page.locator('extensions-manager');
 const toggle=manager.locator('extensions-toolbar').locator('#devMode');
 await toggle.click();
 const load=manager.locator('extensions-toolbar').locator('#loadUnpacked');
 await load.waitFor({state:'visible'});
 const click=load.click();
 await new Promise(r=>setTimeout(r,250));
 execFileSync('xdotool',['key','--clearmodifiers','ctrl+l']);
 execFileSync('xdotool',['key','--clearmodifiers','ctrl+v']);
 execFileSync('xdotool',['key','--clearmodifiers','Return']);
 await click;
 await poll(async()=>{
  const loaded=await manager.locator('extensions-item').evaluateAll(items=>items.map(item=>({id:item.id,name:item.data?.name,version:item.data?.version})));
  if(loaded.some(item=>item.id===id)) return loaded;
  // The native folder chooser navigates on its first Enter and accepts on a
  // second. Both are real key events; no developerPrivate API or profile edit.
  execFileSync('xdotool',['key','--clearmodifiers','Return']);
  return null;
 },'Load unpacked installs the copied folder');
 await poll(async()=>(await call()).connected,'real companion handshake');
 assert.equal((await call()).done,true);
 evidence.stages.push({stage:'installed',id,version:installed.version,path:state.folder,connected:true});
 await call('done');
 await stopApp();
 await context.close(); context=null;
 // A newer synthetic app bundle is prepared on the next real app launch;
 // the installed folder is reused and Chromium reads it on its next startup.
 const updated={...JSON.parse(await readFile(path.join(resource,'manifest.json'))),version:'0.2.3'};
 await writeFile(path.join(resource,'manifest.json'),JSON.stringify(updated));
 launchApp('app-updated');
 await poll(async()=>{
  try {return JSON.parse(await readFile(path.join(state.folder,'manifest.json'))).version==='0.2.3';} catch {return false;}
 },'new app version updates the stable folder');
 const page2=await launchBrowser();
 await page2.goto('chrome://extensions/');
 const item=page2.locator('extensions-manager').locator('extensions-item#'+id);
 await item.waitFor();
 const version=await item.evaluate(item=>item.data.version);
 assert.equal(version,'0.2.3');
 await poll(async()=>(await call()).connected,'updated companion handshake');
 evidence.stages.push({stage:'updated',id,version,path:state.folder,connected:true});
 assert.deepEqual(evidence.storeRequests,[]);
 console.log('PASS actual Chromium Add from Cassini: fresh profile, native picker, pinned ID, handshake and same-path app update');
} catch(error) {
 evidence.error=String(error.stack||error);
 if(context) {
  evidence.pages=context.pages().map(page=>page.url());
  const session=await context.newCDPSession(context.pages()[0]).catch(()=>null);
  if(session) evidence.targets=await session.send('Target.getTargets').catch(()=>null);
  await context.pages()[0]?.screenshot({path:path.join(out,'browser-failure.png')}).catch(()=>{});
 }
 throw error;
} finally {
 if(context) await context.close().catch(()=>{});
 if(app&&app.exitCode===null) app.kill('SIGTERM');
 await writeFile(path.join(out,'companion-install.json'),JSON.stringify(evidence,null,2)+'\n');
}
