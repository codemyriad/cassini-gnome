// An unpacked-extension reload is not a service-worker eviction. Keep the page
// and its MAIN-world WebRTC hook alive while replacing the extension context.
import assert from 'node:assert/strict';
import {generateKeyPairSync, createHash} from 'node:crypto';
import { createServer } from 'node:http';
import { mkdtemp, mkdir, rm, cp, readFile, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { connectReceiveSlots } from '../scenarios/product-topologies.mjs';
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const extensionSource = path.resolve(process.env.CASSINI_TEST_EXTENSION || path.join(root, 'extension'));
const temporary = await mkdtemp(path.join(os.tmpdir(), 'cassini-relay-reload-'));
const profile = path.join(temporary, 'profile'), extension = path.join(temporary, 'extension');
await cp(extensionSource, extension, {recursive:true});
// Installed native hosts allow only the product extension ID. A disposable key
// prevents this browser fixture from ever connecting to the user's app broker.
const manifestPath = path.join(extension,'manifest.json');
const manifest = JSON.parse(await readFile(manifestPath,'utf8'));
const {publicKey} = generateKeyPairSync('rsa',{modulusLength:2048});
const key = publicKey.export({type:'spki',format:'der'});
manifest.key = key.toString('base64');
await writeFile(manifestPath,JSON.stringify(manifest));
const fixtureId = [...createHash('sha256').update(key).digest('hex').slice(0,32)].map(c=>String.fromCharCode(97+parseInt(c,16))).join('');
assert.notEqual(fixtureId,'pcafdaahfhjiolfpfnlhmljfgoabbaij');
const server = createServer((_request, response) => response.end('<!doctype html><title>Meeting relay fixture</title><button>Join</button>'));
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
async function observed(check, label) {
  const deadline = Date.now()+15000;
  while (true) {
    const value = await check();
    if (value) return value;
    assert.ok(Date.now()<deadline, `Timed out waiting for ${label}`);
    await new Promise(resolve=>setTimeout(resolve,25));
  }
}
let context;
try {
  context = await chromium.launchPersistentContext(profile, {
    channel: 'chromium', headless: true,
    args: [`--disable-extensions-except=${extension}`, `--load-extension=${extension}`,
      `--allowlisted-extension-id=${fixtureId}`, '--autoplay-policy=no-user-gesture-required', '--disable-audio-output', '--allow-loopback-in-peer-connection', '--disable-features=WebRtcHideLocalIpsWithMdns']
  });
  const worker = context.serviceWorkers()[0] || await context.waitForEvent('serviceworker');
  const id = new URL(worker.url()).host;
  assert.equal(id,fixtureId);
  const meeting = await context.newPage(), peer = await context.newPage();
  const url = `http://127.0.0.1:${server.address().port}/`;
  await meeting.goto(url); await peer.goto(url);
  await meeting.evaluate(() => {
    window.fixtureDocument = crypto.randomUUID();
    window.discovery = [];
    addEventListener('message', event => {
      if (event.source === window && event.data?.channel === 'cassini-page' && event.data.type === 'discovery') discovery.push(event.data);
    });
  });
  const pageErrors = []; meeting.on('pageerror', error => pageErrors.push(error.message));
  const document = await meeting.evaluate(() => fixtureDocument);
  const tabId = await worker.evaluate(async url => (await chrome.tabs.query({})).find(tab => tab.url === url).id, url);
  const initialBridge = await worker.evaluate(tabId => chrome.tabs.sendMessage(tabId, {type:'cassini-connect'}), tabId);
  assert.equal(initialBridge.connected,true);
  await worker.evaluate(() => { globalThis.fixtureOldWorker = true; });
  const manager = await context.newPage();
  await manager.goto('chrome://extensions/');
  await manager.evaluate(() => chrome.developerPrivate.updateProfileConfiguration({inDeveloperMode:true}));
  await manager.evaluate(id => new Promise((resolve, reject) => {
    chrome.developerPrivate.reload(id, {failQuietly:false}, () => {
      chrome.runtime.lastError ? reject(Error(chrome.runtime.lastError.message)) : resolve();
    });
  }), id);
  async function reloadExtension() {
    await manager.evaluate(id => new Promise((resolve,reject) => chrome.developerPrivate.reload(id,{failQuietly:false},()=>{
      chrome.runtime.lastError ? reject(Error(chrome.runtime.lastError.message)) : resolve();
    })),id);
    const page = await context.newPage();
    await page.goto(`chrome-extension://${id}/popup.html`);
    await page.locator('main[aria-busy="false"]').waitFor();
    return page;
  }
  const popup = await context.newPage();
  await popup.goto(`chrome-extension://${id}/popup.html`);
  await popup.locator('main[aria-busy="false"]').waitFor();
  // Playwright retains the Worker object across an MV3 context restart.
  // Observe the new JS realm rather than waiting for a second worker event.
  const deadline = Date.now() + 15000;
  let fresh;
  while (!fresh) {
    const candidate = context.serviceWorkers().find(item => new URL(item.url()).host === id);
    if (candidate && await candidate.evaluate(() => globalThis.fixtureOldWorker !== true).catch(() => false)) fresh = candidate;
    assert.ok(Date.now() < deadline, 'replacement extension worker realm did not start');
    if (!fresh) await new Promise(resolve => setTimeout(resolve, 25));
  }
  assert.equal(new URL(fresh.url()).host, id);
  await meeting.getByRole('button', {name:'Join'}).click();
  await connectReceiveSlots(meeting, peer, [[880]]);
  await meeting.waitForFunction(() => discovery.some(message => message.sourceCount > 0), null, {timeout:15000});
  assert.equal(await meeting.evaluate(() => fixtureDocument), document, 'meeting tab was never reloaded');
  const bridge = await fresh.evaluate(async tabId => {
    try { return await chrome.tabs.sendMessage(tabId, {type:'cassini-connect'}); }
    catch (error) { return {error:error.message}; }
  }, tabId);
  assert.equal(bridge?.connected, true, `MAIN-world discovery survived, but the replacement worker has no live relay: ${JSON.stringify(bridge)}`);
  // A fixture native sink acknowledges production audio packets in memory.
  // It never invokes an installed app, microphone, filesystem writer or speaker.
  async function installAudioSink(targetWorker) {
  await targetWorker.evaluate(tabId => {
    globalThis.fixtureAudio = [];
    const actualNative = chrome.runtime.connectNative.bind(chrome.runtime);
    chrome.runtime.connectNative = name => {
      if (name !== 'io.codemyriad.cassini') return actualNative(name);
      const event = () => { const listeners = []; return {addListener:fn=>listeners.push(fn),emit:m=>listeners.forEach(fn=>fn(m))}; };
      return {onMessage:event(),onDisconnect:event(),closed:false,
        postMessage(packet) {
          fixtureAudio.push(packet);
          queueMicrotask(() => this.onMessage.emit(packet.type === 'start' ? {type:'started',path:'/fixture/Meeting',features:packet.features || []} :
            packet.type === 'stop' ? {type:'stopped',path:'/fixture/Meeting'} : {type:'ack'}));
        }, disconnect() { if (!this.closed) { this.closed=true;this.onDisconnect.emit(); } }};
    };
    const actualQuery = chrome.tabs.query.bind(chrome.tabs);
    chrome.tabs.query = async query => query.active ? [await chrome.tabs.get(tabId)] : actualQuery(query);
  }, tabId);
  }
  await installAudioSink(fresh);
  const status = () => popup.evaluate(() => chrome.runtime.sendMessage({target:'background',type:'status'}));
  let current = await observed(async () => { const value = await status(); return value.browserHealth?.filesOk === undefined ? null : value; }, "packaged-file self-check");
  assert.equal(current.browserHealth.state, 'ready');
  assert.equal(current.browserHealth.filesOk,true);
  assert.equal(current.browserHealth.installType,'development');
  assert.equal(current.browserHealth.extensionVersion,manifest.version);
  assert.notEqual(current.browserHealth.extensionInstance,initialBridge.instanceId,'reload replaces the extension instance');
  assert.equal(current.browserHealth.tabs.find(tab => tab.tabId === tabId).relayAlive, true);
  // Repeated repair in a healthy realm must keep one relay and capture path.
  await fresh.evaluate(async tabId => {
    for (let i=0;i<2;i++) await chrome.scripting.executeScript({target:{tabId,allFrames:true},files:['relay.js'],world:'ISOLATED'});
  }, tabId);
  await meeting.evaluate(() => {
    window.captureLifecycle = [];
    const close = AudioContext.prototype.close;
    AudioContext.prototype.close = function() {
      captureLifecycle.push({type:'contextClose',state:this.state});
      return close.call(this);
    };
    addEventListener('message', event => {
      if (event.source !== window) return;
      const {channel,type,token,instanceId} = event.data || {};
      if (channel === 'cassini-control' && ['start','stop'].includes(type) ||
          channel === 'cassini-page' && ['stopped','hookReady'].includes(type))
        captureLifecycle.push({type,token,instanceId});
    });
  });
  // This fixture opens the popup as a full extension page. Supply the meeting
  // as the browser-owned active tab that an actual toolbar popup would see.
  await popup.evaluate(tabId => {
    const query = chrome.tabs.query.bind(chrome.tabs);
    chrome.tabs.query = async options => options.active ? [await chrome.tabs.get(tabId)] : query(options);
  }, tabId);
  await popup.getByRole('heading',{name:'Interactive session is unverified',exact:true}).waitFor();
  await meeting.waitForFunction(() => discovery.some(row => row.sourceCount > 0 && row.heard === true));
  const beforeStart = await popup.evaluate(tabId => chrome.runtime.sendMessage({target:'background',type:'status',tabId}), tabId);
  assert.ok(await meeting.evaluate(() => discovery.some(row => row.sourceCount > 0 && row.heard === true)),
    'reload repair must preserve the live RTP inventory without granting admission');
  assert.equal(beforeStart.tabEvidence.transport.page, 'ready');
  assert.equal(beforeStart.tabEvidence.callEvidence.state, 'unknown');
  console.log('Reload repair observation:', JSON.stringify({sourceCount:await meeting.evaluate(() => discovery.at(-1).sourceCount),
    heard:await meeting.evaluate(() => discovery.at(-1).heard),callEvidence:beforeStart.tabEvidence.callEvidence.state}));
  const started = await popup.evaluate(() => chrome.runtime.sendMessage({target:'background',type:'start',mode:'tracks'}));
  assert.equal(started.recording, true, JSON.stringify(started));
  const audioDeadline = Date.now() + 15000;
  let audio;
  do {
    audio = await fresh.evaluate(() => fixtureAudio.filter(packet => packet.type === 'audio').map(packet => packet.pcm));
    assert.ok(Date.now() < audioDeadline, `new worker received no PCM after reload: ${JSON.stringify(await status())}`);
    if (audio.reduce((n,value)=>n+Buffer.from(value,'base64').length,0) < 48000)
      await new Promise(resolve => setTimeout(resolve,25));
  } while (audio.reduce((n,value)=>n+Buffer.from(value,'base64').length,0) < 48000);
  const pcm = Buffer.concat(audio.map(value => Buffer.from(value,'base64')));
  let power = 0;
  for (let i=0;i<pcm.length;i+=2) power += (pcm.readInt16LE(i)/32768)**2;
  assert.ok(power/(pcm.length/2) > 0.001, 'reconnected worker must receive non-silent remote audio');
  await popup.getByRole('heading',{name:'Browser capture is active',exact:true}).waitFor();
  assert.equal(await popup.locator('#reload').isVisible(),false);
  assert.equal(await popup.getByRole('button',{name:/record|stop/i}).count(),0);
  await mkdir(path.join(root,'artifacts'),{recursive:true});
  await popup.locator('body').screenshot({path:path.join(root,'artifacts/popup-loaded-extension.png')});
  // Reload while production capture is active and the generated peer keeps
  // sending. The old relay must drain once before the same tab records again.
  const departingToken = await meeting.evaluate(() => captureLifecycle.find(m=>m.type==='start').token);
  const activeReloadPopup = await reloadExtension();
  await meeting.waitForFunction(token=>captureLifecycle.some(m=>m.type==='stopped'&&m.token===token),departingToken);
  const lifecycle = await meeting.evaluate(() => captureLifecycle);
  assert.equal(lifecycle.filter(m=>m.type==='stop'&&m.token===departingToken).length,1,'reload posts a single stop');
  assert.equal(lifecycle.filter(m=>m.type==='stopped'&&m.token===departingToken).length,1,'departing capture drains once');
  assert.equal(lifecycle.filter(m=>m.type==='contextClose').length,1,'shared context closes once after flush');
  assert.equal(lifecycle.find(m=>m.type==='contextClose').state,'running');
  const activeWorker = context.serviceWorkers().find(item=>new URL(item.url()).host===id);
  const readyAfterDrain = await observed(async()=>{
    const status=await activeReloadPopup.evaluate(()=>chrome.runtime.sendMessage({target:'background',type:'status'}));
    return status.browserHealth?.tabs.some(tab=>tab.tabId===tabId&&tab.state==='ready') && status;
  },'replacement relay ready after active capture drains');
  const readyEvent = await meeting.evaluate(instanceId=>captureLifecycle.findIndex(m=>m.type==='hookReady'&&m.instanceId===instanceId),readyAfterDrain.browserHealth.extensionInstance);
  assert.ok(readyEvent>lifecycle.findIndex(m=>m.type==='stopped'&&m.token===departingToken));
  await installAudioSink(activeWorker);
  const restarted = await activeReloadPopup.evaluate(()=>chrome.runtime.sendMessage({target:'background',type:'start',mode:'tracks'}));
  assert.equal(restarted.recording,true,JSON.stringify(restarted));
  await observed(()=>activeWorker.evaluate(()=>fixtureAudio.some(p=>p.type==='audio'&&p.pcm)), 'replacement capture PCM after active reload');
  const stopped = await activeReloadPopup.evaluate(() => chrome.runtime.sendMessage({target:'background',type:'stop'}));
  assert.equal(stopped.recording,false);
  assert.equal(await meeting.evaluate(() => fixtureDocument), document);
  assert.deepEqual(pageErrors, []);
  console.log('PASS unpacked extension reload: persistent meeting hook, versioned relay readiness, non-silent remote PCM reaches the replacement worker');
  console.log('PASS reload during active recording: single stop and context close, drained hook readiness, same meeting records again without page errors');
  // Upgrade a page whose MAIN hook predates the readiness protocol. Install a
  // no-handshake hook only in the disposable unpacked copy, then replace the
  // extension again. Reinjection must not wrap that live hook a second time.
  const hookPath = path.join(extension,'page-hook.js');
  const compatibleHook = await readFile(hookPath,'utf8');
  const legacyHook = compatibleHook.replace('message.protocol === 1', 'message.protocol === 0');
  assert.notEqual(legacyHook,compatibleHook);
  await writeFile(hookPath,legacyHook);
  await reloadExtension();
  const legacyMeeting = await context.newPage(), legacyPeer = await context.newPage();
  await legacyMeeting.goto(url+'legacy'); await legacyPeer.goto(url+'peer');
  await legacyMeeting.getByRole('button',{name:'Join'}).click();
  await connectReceiveSlots(legacyMeeting,legacyPeer,[[660]]);
  await writeFile(hookPath,compatibleHook);
  const recoveredPopup = await reloadExtension();
  const legacyTabId = await recoveredPopup.evaluate(async url => (await chrome.tabs.query({})).find(tab=>tab.url===url).id,url+'legacy');
  const broken = await observed(async () => {
    const tab = await recoveredPopup.evaluate(async tabId => {
      const status = await chrome.runtime.sendMessage({target:'background',type:'status'});
      return status.browserHealth?.tabs.find(tab=>tab.tabId===tabId);
    },legacyTabId);
    return tab?.state==='reloadRequired' && tab.message ? tab : null;
  }, 'incompatible live hook reload guidance');
  assert.equal(broken.hookSeen,true);assert.equal(broken.relayAlive,true);
  assert.equal(broken.message,'Reload the meeting tab so Cassini can record it.',JSON.stringify(broken));
  const reloaded = legacyMeeting.waitForEvent('load');
  await recoveredPopup.evaluate(tabId=>chrome.runtime.sendMessage({target:'background',type:'reloadMeetingTab',tabId}),legacyTabId);
  await reloaded;
  await legacyMeeting.getByRole('button',{name:'Join'}).click();
  await connectReceiveSlots(legacyMeeting,legacyPeer,[[660]]);
  await observed(() => recoveredPopup.evaluate(async tabId => {
    const status = await chrome.runtime.sendMessage({target:'background',type:'status'});
    return status.browserHealth?.tabs.some(tab=>tab.tabId===tabId&&tab.state==='ready');
  },legacyTabId), 'reloaded and rejoined meeting readiness');
  console.log('PASS incompatible live hook: explicit reload guidance, user reload action, rejoined meeting becomes ready');
  // Missing packaged files are self-check diagnostics, independent of tab
  // readiness. Mutate only the disposable unpacked copy, never the checkout.
  await rm(path.join(extension,'pcm-worklet.js'));
  const missingFilePopup = await reloadExtension();
  const missingFile = await observed(() => missingFilePopup.evaluate(async () => {
    const status = await chrome.runtime.sendMessage({target:'background',type:'status'});
    return status.browserHealth?.filesOk === false ? status.browserHealth : null;
  }), 'missing packaged worklet diagnostic');
  assert.equal(missingFile.installType,'development');
  assert.equal(missingFile.extensionVersion,manifest.version);
  console.log('PASS companion self-check: unpacked install, runtime version, healthy resources and missing worklet diagnostics');
} finally {
  await context?.close(); server.close(); await rm(temporary, {recursive:true,force:true});
}
