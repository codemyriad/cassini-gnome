// Real Nextcloud Talk + shipped extension: what the toolbar popup says when opened on a call tab.
//  1. Joined unmuted from the start: opening the popup reports an interactive session.
//  2. A tab the extension only reaches after the page loaded (no document-start relay, as for a tab
//     open across an extension install/update): the popup connects on open and asks for a reload
//     instead of the misleading "unverified" listen-only message. Disposable loopback rooms only.
import assert from 'node:assert/strict';
import {cp,mkdtemp,readFile,writeFile,rm} from 'node:fs/promises';
import os from 'node:os';import path from 'node:path';import {fileURLToPath} from 'node:url';
import {joinNextcloudTalk} from '../scenarios/nextcloud-talk.mjs';
import {waitForHeading} from './support/popup-check.mjs';
const {chromium}=await import(process.env.PLAYWRIGHT_MODULE||'playwright');
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const callURL=process.env.NEXTCLOUD_TEST_CALL_URL,otherURL=process.env.NEXTCLOUD_TEST_OTHER_CALL_URL;
assert.ok(callURL&&otherURL,'NEXTCLOUD_TEST_CALL_URL and NEXTCLOUD_TEST_OTHER_CALL_URL must name disposable rooms');
for(const url of [callURL,otherURL])assert.ok(['127.0.0.1','localhost','[::1]'].includes(new URL(url).hostname),'Automated tests must not join public rooms');
const temporary=await mkdtemp(path.join(os.tmpdir(),'cassini-popup-open-'));
const shared=['--autoplay-policy=no-user-gesture-required','--disable-audio-output','--use-fake-device-for-media-stream','--use-fake-ui-for-media-stream'];
const userAgent='Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36';
async function wave(frequency){
 const rate=48000,frames=rate*8,data=Buffer.alloc(44+frames*2);data.write('RIFF');data.writeUInt32LE(data.length-8,4);data.write('WAVEfmt ',8);data.writeUInt32LE(16,16);data.writeUInt16LE(1,20);data.writeUInt16LE(1,22);data.writeUInt32LE(rate,24);data.writeUInt32LE(rate*2,28);data.writeUInt16LE(2,32);data.writeUInt16LE(16,34);data.write('data',36);data.writeUInt32LE(frames*2,40);
 for(let i=0;i<frames;i++)data.writeInt16LE(Math.round(6000*Math.sin(2*Math.PI*frequency*i/rate)),44+i*2);
 const file=path.join(temporary,'input-'+frequency+'.wav');await writeFile(file,data);return file;
}
async function scenario(name,{room,stripRelay,expected,roster}){
 const extension=path.join(temporary,name,'extension');await cp(path.join(root,'extension'),extension,{recursive:true});
 const manifestPath=path.join(extension,'manifest.json'),manifest=JSON.parse(await readFile(manifestPath,'utf8'));
 if(stripRelay)manifest.content_scripts=manifest.content_scripts.filter(entry=>!entry.js.includes('relay.js'));
 await writeFile(manifestPath,JSON.stringify(manifest));
 const others=[];let context;
 try{
  context=await chromium.launchPersistentContext(path.join(temporary,name,'profile'),{channel:'chromium',headless:true,userAgent,permissions:['microphone','camera'],
   args:[...shared,`--disable-extensions-except=${extension}`,`--load-extension=${extension}`,`--use-file-for-fake-audio-capture=${await wave(300)}`]});
  const worker=context.serviceWorkers()[0]||await context.waitForEvent('serviceworker');
  const meeting=await context.newPage();
  await joinNextcloudTalk(meeting,{url:room,name:'Cassini receiver',muted:false});
  for(const [who,frequency] of [['Hypatia',440],['Ibn al-Haytham',880]]){
   const browser=await chromium.launch({channel:'chromium',headless:true,args:[...shared,`--use-file-for-fake-audio-capture=${await wave(frequency)}`]});others.push(browser);
   const other=await browser.newContext({permissions:['microphone','camera'],userAgent});
   await joinNextcloudTalk(await other.newPage(),{url:room,name:who});
  }
  const popup=await context.newPage();
  const id=new URL(worker.url()).host;
  await popup.goto(`chrome-extension://${id}/popup.html`);
  // The popup action page is not the active tab; point its tab query at the real meeting tab.
  const meetingUrl=meeting.url();
  await popup.evaluate(url=>{const query=chrome.tabs.query.bind(chrome.tabs);
   chrome.tabs.query=async options=>options.active?(await query({})).filter(tab=>tab.url===url).slice(0,1):query(options);},meetingUrl);
  await waitForHeading(popup,expected,25000);
  const title=await popup.locator('#status').textContent();
  if(roster){ // Display-only roster from Talk's own cards, shown before any recording starts.
   const list=async()=>(await popup.locator('#participant-list li').allTextContents()).sort();
   const until=Date.now()+15000;while(Date.now()<until&&JSON.stringify(await list())!==JSON.stringify(roster)){await popup.locator('#recheck').click();await new Promise(resolve=>setTimeout(resolve,500));}
   assert.deepEqual(await list(),roster,'the popup names the remote participants before recording');
   assert.equal(await popup.locator('#participants').isVisible(),true);
   assert.doesNotMatch(await popup.locator('#diagnostics').textContent(),/Hypatia|Haytham/,'names stay out of the copied diagnostics');
  }
  return title;
 }finally{await context?.close();for(const browser of others)await browser.close();}
}
try{
 assert.equal(await scenario('trusted',{room:callURL,stripRelay:false,expected:'Interactive media session detected',roster:['Hypatia','Ibn al-Haytham']}),'Interactive media session detected');
 assert.equal(await scenario('reopened',{room:otherURL,stripRelay:true,expected:'Reload this tab for automatic detection'}),'Reload this tab for automatic detection');
 console.log('PASS popup on a real Talk call: interactive session and participant names when trusted, reload prompt when the relay arrived after the page');
}finally{await rm(temporary,{recursive:true,force:true});}
