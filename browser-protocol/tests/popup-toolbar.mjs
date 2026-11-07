// Exercise Chrome's real action-popup sizing, not a popup.html browser tab.
// A disposable extension identity/profile cannot connect to installed app hosts.
import assert from 'node:assert/strict';
import {createHash, generateKeyPairSync} from 'node:crypto';
import {createServer} from 'node:http';
import {cp, mkdir, mkdtemp, readFile, rm, writeFile} from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
const {chromium} = await import(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const temporary=await mkdtemp(path.join(os.tmpdir(),'cassini-popup-toolbar-'));
const extension=path.join(temporary,'extension');
await cp(path.resolve(process.env.CASSINI_TEST_EXTENSION || path.join(root,'extension')),extension,{recursive:true});
const manifestPath=path.join(extension,'manifest.json');
const manifest=JSON.parse(await readFile(manifestPath,'utf8'));
const {publicKey}=generateKeyPairSync('rsa',{modulusLength:2048});
const key=publicKey.export({type:'spki',format:'der'});manifest.key=key.toString('base64');
await writeFile(manifestPath,JSON.stringify(manifest));
const id=[...createHash('sha256').update(key).digest('hex').slice(0,32)].map(c=>String.fromCharCode(97+parseInt(c,16))).join('');
assert.notEqual(id,'pcafdaahfhjiolfpfnlhmljfgoabbaij');
const server=createServer((_request,response)=>response.end('<!doctype html><title>Generated meeting with a long title to check the companion toolbar popup layout</title>Meeting fixture'));
await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
const zoom=Number(process.env.POPUP_ZOOM || 1);
assert.ok([1,3].includes(zoom),'fixture supports default or 300% browser zoom');
let context;
try {
  context=await chromium.launchPersistentContext(path.join(temporary,'profile'),{
    channel:'chromium',headless:process.env.POPUP_HEADED!=='1',viewport:null,
    args:[`--disable-extensions-except=${extension}`,`--load-extension=${extension}`,`--allowlisted-extension-id=${id}`,'--window-size=1200,900',...(process.env.POPUP_HEADED==='1'?['--ozone-platform=x11']:[])]
  });
  const worker=context.serviceWorkers()[0]||await context.waitForEvent('serviceworker');
  const meeting=await context.newPage();
  await meeting.goto(`http://127.0.0.1:${server.address().port}/`);
  await meeting.bringToFront();
  const unzoomedDpr=await meeting.evaluate(()=>devicePixelRatio);
  // Action popups use Chrome's default zoom, not a tab's per-origin zoom.
  // Change it only through Settings in this disposable browser profile.
  if(zoom!==1){
    const settings=await context.newPage();
    await settings.goto('chrome://settings/appearance');
    const applied=await settings.evaluate(async factor=>{
      await new Promise((resolve,reject)=>chrome.settingsPrivate.setDefaultZoom(factor,()=>{
        chrome.runtime.lastError?reject(Error(chrome.runtime.lastError.message)):resolve();
      }));
      return new Promise(resolve=>chrome.settingsPrivate.getDefaultZoom(resolve));
    },zoom);
    assert.equal(applied,zoom,'Chrome must apply actual default browser zoom');
    await settings.close();
    await meeting.bringToFront();
  }
  const browserSession=await context.browser().newBrowserCDPSession();
  await worker.evaluate(async()=>{
    const window=await chrome.windows.getLastFocused();
    await chrome.windows.update(window.id,{focused:true});
    await chrome.action.openPopup({windowId:window.id});
  });
  const targets=(await browserSession.send('Target.getTargets')).targetInfos;
  const target=targets.find(t=>t.url===`chrome-extension://${id}/popup.html`);
  assert.ok(target,'Chrome must create an actual toolbar popup target');
  const {sessionId}=await browserSession.send('Target.attachToTarget',{targetId:target.targetId,flatten:false});
  let nextId=0;
  async function popupCall(method,params={}) {
    const requestId=++nextId;
    const reply=new Promise((resolve,reject)=>{
      const timer=setTimeout(()=>{browserSession.off('Target.receivedMessageFromTarget',receive);reject(Error(`No reply from popup: ${method}`));},10000);
      function receive(event){
        if(event.sessionId!==sessionId)return;
        const message=JSON.parse(event.message);
        if(message.id!==requestId)return;
        clearTimeout(timer);browserSession.off('Target.receivedMessageFromTarget',receive);
        if(message.error)reject(Error(JSON.stringify(message.error)));else resolve(message.result);
      }
      browserSession.on('Target.receivedMessageFromTarget',receive);
    });
    await browserSession.send('Target.sendMessageToTarget',{sessionId,message:JSON.stringify({id:requestId,method,params})});
    return reply;
  }
  const ready=await popupCall('Runtime.evaluate',{expression:`new Promise((resolve,reject)=>{
    const done=()=>document.querySelector('main[aria-busy="false"]');
    if(done()){resolve();return;}
    const observer=new MutationObserver(()=>{if(done()){observer.disconnect();clearTimeout(timer);resolve();}});
    const timer=setTimeout(()=>{observer.disconnect();reject(Error('Popup did not finish checking'));},8000);
    observer.observe(document.documentElement,{subtree:true,attributes:true});
  })`,awaitPromise:true});
  assert.ok(!ready.exceptionDetails,JSON.stringify(ready));
  const geometry=await popupCall('Runtime.evaluate',{expression:`new Promise((resolve,reject)=>{
    let previous=null,unchanged=0;const deadline=performance.now()+3000;
    function measure(){
      const size={viewport:innerWidth,body:document.body.getBoundingClientRect().width,
        content:document.documentElement.scrollWidth,devicePixelRatio,zoom:${zoom},title:document.getElementById('tab-title').getBoundingClientRect().height};
      const signature=JSON.stringify(size);
      unchanged=signature===previous?unchanged+1:0;previous=signature;
      if(unchanged>=3){resolve(size);return;}
      if(performance.now()>=deadline){reject(Error('Toolbar popup size did not settle'));return;}
      requestAnimationFrame(measure);
    }
    requestAnimationFrame(measure);
  })`,awaitPromise:true,returnByValue:true});
  assert.ok(!geometry.exceptionDetails,JSON.stringify(geometry));
  const size=geometry.result.value;
  assert.ok(Math.abs(size.devicePixelRatio/unzoomedDpr-zoom)<0.01,'actual popup must use the requested browser zoom');
  console.log('Toolbar popup geometry:',JSON.stringify(size));
  await mkdir(path.join(root,'artifacts'),{recursive:true});
  const screenshot=await popupCall('Page.captureScreenshot');
  await writeFile(path.join(root,zoom===1?'artifacts/popup-toolbar.png':'artifacts/popup-toolbar-300.png'),Buffer.from(screenshot.data,'base64'));
  if(zoom===1)assert.ok(size.viewport>=376,`toolbar popup collapsed to ${size.viewport}px instead of 376px`);
  if(zoom===1)assert.equal(size.body,376);
  else assert.ok(size.viewport<376,'high zoom must constrain the real action popup');
  assert.ok(size.body<=size.viewport,'popup body must shrink to Chrome’s constrained viewport');
  assert.ok(size.content<=size.viewport,'popup content must not overflow horizontally');
  console.log(`PASS real toolbar popup at ${zoom*100}%: preferred or constrained width, no horizontal overflow`);
} finally {await context?.close();await new Promise(resolve=>server.close(resolve));await rm(temporary,{recursive:true,force:true});}
