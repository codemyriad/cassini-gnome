// Real extension -> native hosts -> broker -> Library, using private devices only.
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {spawn, execFileSync} from 'node:child_process';
import {createServer} from 'node:http';
import {mkdtemp, mkdir, readFile, readdir, writeFile, chmod, rm} from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import {createInterface} from 'node:readline';
import {createWriteStream} from 'node:fs';
import {chromium} from 'playwright';
assert.equal(process.env.CASSINI_ISOLATED_AUDIO, '1', 'Use private recording harness');
assert.ok(!process.env.WAYLAND_DISPLAY);
assert.match(process.env.PULSE_SERVER || '', /^unix:\/tmp\/cassini-audio-/);
const root = path.resolve(import.meta.dirname, '../..');
const artifacts = process.env.CASSINI_TEST_ARTIFACTS;
process.env.CASSINI_LIBRARY_DIR = artifacts;
const extension = process.env.CASSINI_TEST_EXTENSION || path.join(root, 'browser-extension');
const build = process.env.CASSINI_TEST_BUILD || path.join(root, '_build');
const profile = await mkdtemp(path.join(os.tmpdir(), 'cassini-app-control-'));
const fixture = await readFile(path.join(root, 'tests/recording/fixture.html'));
const server = createServer((_req, res) => {res.setHeader('Content-Type', 'text/html'); res.end(fixture);});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const evidence = {scenarios: [], console: []}, pending = [], pairs = [];
let context, remoteBrowser, ui, microphone;
async function poll(check, label, timeout=20000) {
 const end = Date.now() + timeout;
 while (Date.now() < end) {const value = await check(); if (value) return value; await new Promise(r => setTimeout(r, 100));}
 throw new Error(`Timed out: ${label}`);
}
function call(packet={action: 'status'}) {
 return new Promise((resolve, reject) => {
  const timer = setTimeout(() => reject(new Error('GNOME UI request timed out')), 12000);
  pending.push(value => {clearTimeout(timer); value.error ? reject(new Error(value.error)) : resolve(value);});
  ui.stdin.write(JSON.stringify(packet) + '\n');
 });
}
async function hostManifest(name, script, id) {
 const file = path.join(profile, name + '.py'); await writeFile(file, script); await chmod(file, 0o700);
 await writeFile(path.join(profile, 'NativeMessagingHosts', name + '.json'), JSON.stringify({name, description: 'Isolated app test', path: file, type: 'stdio', allowed_origins: [`chrome-extension://${id}/`]}));
}
async function samples(directory) {
 const manifest = JSON.parse(await readFile(path.join(directory, 'session.json'))), tracks = [];
 for (const track of manifest.tracks) {
  const bytes = await readFile(path.join(directory, track.file));
  tracks.push({...track, frames: (bytes.length - 44) / (track.channels * 2), bytes});
 }
 return {manifest, tracks};
}
function power(track, frequency) {
 const count = Math.min(track.frames, 48000), start = track.frames - count;
 let real = 0, imaginary = 0;
 for (let i=0; i<count; i++) {
  const sample = track.bytes.readInt16LE(44 + (start + i) * track.channels * 2) / 32768;
  const angle = 2 * Math.PI * frequency * i / track.sampleRate;
  real += sample * Math.cos(angle); imaginary += sample * Math.sin(angle);
 }
 return (real * real + imaginary * imaginary) / (count * count);
}
async function join(title, localHz, remoteHz, id) {
 const meeting = await context.newPage(), remote = await remoteBrowser.newPage();
 pairs.push({meeting, remote});
 for (const page of [meeting, remote]) {
  await page.addInitScript(() => {
   window.cassiniDiscoveryEvidence = [];
   window.addEventListener('message', event => {
    if (event.source === window && event.data?.channel === 'cassini-page' && event.data.type === 'discovery') {
     cassiniDiscoveryEvidence.push({sourceCount: event.data.sourceCount, heard: event.data.heard});
     if (cassiniDiscoveryEvidence.length > 8) cassiniDiscoveryEvidence.shift();
    }
   });
  });
  await page.goto(`http://127.0.0.1:${server.address().port}/`); await page.click('#join');
 }
 await meeting.evaluate(async ({title, localHz}) => {document.title = title; await window.join(localHz);}, {title, localHz});
 await remote.evaluate(hz => window.join(hz), remoteHz);
 const offer = await meeting.evaluate(async () => {await meeting.pc.setLocalDescription(await meeting.pc.createOffer()); return meeting.description();});
 const answer = await remote.evaluate(async offer => {await meeting.pc.setRemoteDescription(offer); await meeting.pc.setLocalDescription(await meeting.pc.createAnswer()); return meeting.description();}, offer);
 await meeting.evaluate(answer => meeting.pc.setRemoteDescription(answer), answer);
 await poll(async () => (await call()).meetings.some(m => m.title === title), `app discovers ${title}`);
 const worker = context.serviceWorkers().find(worker => worker.url().includes(id)) || await context.waitForEvent('serviceworker');
 await worker.evaluate(async title => {const tab = (await chrome.tabs.query({})).find(tab => tab.title === title); await chrome.tabs.update(tab.id, {muted: true});}, title);
 return {meeting, remote};
}
async function automatic(enabled) {
 await call({action: 'show-settings'});
 await poll(async () => (await call()).settingsReady, 'Settings reads real browser preference');
 await call({action: 'auto', enabled});
 await poll(async () => (await call()).auto === enabled, 'automatic preference applied');
}
async function record() {
 await poll(async () => (await call()).recordReady, 'previous capture fully saved before Record starts');
 await call({action: 'record'});
}
async function recording(title) {
 const status = await poll(async () => (await call()).statuses.find(s => s.recording && s.title === title && s.path), `recording ${title}`);
 await poll(async () => (await samples(status.path)).tracks.some(t => t.kind === 'remote' && t.frames >= t.sampleRate * 2), `two seconds of ${title}`);
 return status.path;
}
try {
 const manifest = JSON.parse(await readFile(path.join(extension, 'manifest.json')));
 assert.ok(manifest.key, 'the companion has a pinned key');
 const id = [...createHash('sha256').update(Buffer.from(manifest.key, 'base64')).digest('hex').slice(0, 32)].map(c => String.fromCharCode(97 + parseInt(c, 16))).join('');
 assert.equal(id, 'pcafdaahfhjiolfpfnlhmljfgoabbaij');
 await mkdir(path.join(profile, 'NativeMessagingHosts'), {recursive: true});
 await hostManifest('io.codemyriad.cassini', `#!/usr/bin/python3\nimport sys\nsys.path.insert(0,${JSON.stringify(root + '/src')})\nfrom cassini_viewer.browser_host import main\nsys.exit(main(directory=${JSON.stringify(artifacts)},open_completed=False))\n`, id);
 await hostManifest('io.codemyriad.cassini.control', `#!/usr/bin/python3\nimport sys\nsys.path.insert(0,${JSON.stringify(root + '/src')})\nsys.stderr=open(${JSON.stringify(path.join(artifacts, 'control-host.log'))},'a',buffering=1)\nfrom cassini_viewer.browser_control_host import main\nsys.exit(main())\n`, id);
 ui = spawn('/usr/bin/python3', [path.join(root, 'tests/recording/browser-ui.py'), '--build', build], {stdio: ['pipe', 'pipe', 'pipe'], env: {...process.env}});
 ui.stderr.pipe(createWriteStream(path.join(artifacts, 'gnome-ui.log')));
 createInterface({input: ui.stdout}).on('line', line => {try {pending.shift()?.(JSON.parse(line));} catch {evidence.console.push(line);}});
 ui.on('exit', code => {evidence.uiExit = code;});
 await poll(async () => (await call()).shortcutBound, 'private portal shortcut bound before browser startup');
 microphone = spawn('/usr/bin/python3', ['-c', `import gi,signal\ngi.require_version('Gst','1.0')\nfrom gi.repository import Gst,GLib\nGst.init(None)\np=Gst.parse_launch('audiotestsrc is-live=true freq=660 volume=0.2 ! audioconvert ! audioresample ! audio/x-raw,rate=48000,channels=1 ! pulsesink device=cassini_microphone sync=false')\nloop=GLib.MainLoop()\nsignal.signal(signal.SIGTERM,lambda *_:loop.quit())\np.set_state(Gst.State.PLAYING)\ntry: loop.run()\nfinally:p.set_state(Gst.State.NULL)\n`], {stdio: ['ignore', 'ignore', 'pipe']});
 microphone.stderr.pipe(createWriteStream(path.join(artifacts, 'microphone-producer.log')));
 execFileSync('pactl', ['--server', process.env.PULSE_SERVER, 'set-sink-mute', 'cassini_desktop', '1']);
 assert.match(execFileSync('pactl', ['--server', process.env.PULSE_SERVER, 'get-sink-mute', 'cassini_desktop'], {encoding: 'utf8'}), /yes/);
 const flags = ['--no-sandbox', '--ozone-platform=x11', '--autoplay-policy=no-user-gesture-required', '--allow-loopback-in-peer-connection', '--disable-features=WebRtcHideLocalIpsWithMdns'];
 context = await chromium.launchPersistentContext(profile, {channel: 'chromium', headless: false, ignoreDefaultArgs: ['--mute-audio'], args: [`--disable-extensions-except=${extension}`, `--load-extension=${extension}`, ...flags]});
 const worker = context.serviceWorkers()[0] || await context.waitForEvent('serviceworker');
 assert.equal(new URL(worker.url()).host, id);
 worker.on('console', message => evidence.console.push(message.text()));
 await poll(async () => (await call()).connected > 0, 'actual companion handshake in Library');
 remoteBrowser = await chromium.launch({headless: false, ignoreDefaultArgs: ['--mute-audio'], args: flags});
 await join('First browser meeting', 440, 880, id);
 await join('Second browser meeting', 550, 990, id);
 const distraction = await context.newPage(); await distraction.goto(`http://127.0.0.1:${server.address().port}/?not-a-meeting`); await distraction.bringToFront();
 await record(); // The real Library Record action snapshots both calls.
 const directories = [await recording('First browser meeting'), await recording('Second browser meeting')];
 assert.notEqual(...directories);
 const powers = [];
 for (let i=0; i<directories.length; i++) {
  const audio = await samples(directories[i]);
  const microphone = audio.tracks.find(t => t.kind === 'microphone'), remote = audio.tracks.find(t => t.kind === 'remote');
  assert.ok(microphone && remote && !audio.tracks.some(t => t.kind === 'local'), 'native mic copy plus browser remote, without page-local duplication');
  const wanted = power(remote, [880, 990][i]), bleed = power(remote, [990, 880][i]), mic = power(microphone, 660);
  assert.ok(wanted > .001 && bleed < wanted * .01 && mic > .001, `muted source/mic isolation: ${JSON.stringify({wanted, bleed, mic})}`);
  powers.push({wanted, bleed, mic});
  const live = await poll(async () => (await call()).library.find(m => m.path === directories[i] && m.lifecycle === 'recording'), 'real live Library row');
  assert.equal(live.interruptedAtMs, null); assert.equal(live.header, 'live');
 }
 await join('Later call stays manual', 770, 1210, id);
 const manual = await call();
 assert.equal(manual.meetings.length, 3);
 assert.equal(manual.statuses.filter(s => s.recording).length, 2, 'a later call must not join manual Record');
 await call({action: 'reopen'});
 execFileSync('import', ['-window', 'root', path.join(artifacts, 'app-recording.png')]);
 await call({action: 'stop', path: directories[0]});
 await poll(async () => (await call()).library.some(m => m.path === directories[0] && m.lifecycle === 'saved'), 'individual page Stop saves only first');
 const frozen = (await samples(directories[0])).tracks.map(t => t.frames);
 const secondFrames = (await samples(directories[1])).tracks.find(t => t.kind === 'remote').frames;
 await poll(async () => (await samples(directories[1])).tracks.find(t => t.kind === 'remote').frames >= secondFrames + 48000, 'other session keeps receiving a full second');
 assert.deepEqual((await samples(directories[0])).tracks.map(t => t.frames), frozen);
 await call({action: 'stop-all'});
 await poll(async () => !(await call()).statuses.some(s => s.recording), 'global pill stops remaining session');
 for (const directory of directories) {
  const saved = await samples(directory);
  assert.equal(saved.manifest.state, 'completed');
  const ends=(saved.manifest.events || []).filter(event => event.type === 'end');
  assert.equal(ends.length,1,'manual Stop checkpoints exactly one terminal session event');
  assert.equal(ends[0].reason,'user');
  assert.ok(saved.manifest.tracks.every(track=>track.endOffsetMs===null || ends[0].atMs>=track.endOffsetMs),'terminal event follows every finalized known track end');
  await poll(async () => {
   try {
    const metadata = JSON.parse(await readFile(path.join(directory, 'meeting.json')));
    return metadata.recordingDetails?.end?.reason === 'user' && metadata.recordingDetails.end.atMs > 0;
   } catch (error) {if (error.code === 'ENOENT') return false; throw error;}
  }, 'manual browser Stop saves app-owned end facts');
 }
 evidence.scenarios.push({name: 'library-records-two-active-calls-muted-tab-and-sink-independent-mic-stop', directories, powers});
 await automatic(true);
 await poll(async () => (await call()).statuses.filter(s => s.recording).length === 3, 'auto opt-in starts every current call');
 await call({action: 'shortcut'});
 await poll(async () => !(await call()).statuses.some(s => s.recording), 'actual portal shortcut stops all sessions');
 await automatic(false);
 await automatic(true);
 await poll(async () => (await call()).statuses.filter(s => s.recording).length === 3, 'auto re-enabled for hidden capture');
 await call({action: 'close-window'});
 await poll(async () => (await call()).status === 'Window closed', 'desktop approves background capture');
 const hidden = await poll(async () => (await call()).notifications.recording, 'native hidden notification');
 assert.ok(hidden.buttons.some(button => button.action === 'app.stop-recording'));
 await call({action: 'notification-stop'});
 await poll(async () => !(await call()).statuses.some(s => s.recording) && !(await call()).notifications.recording, 'notification Stop stops all hidden captures');
 for (const pair of pairs) {await pair.meeting.evaluate(() => meeting.pc.close()); await pair.remote.evaluate(() => meeting.pc.close());}
 await poll(async () => !(await call()).meetings.length, 'all previous calls end while hidden');
 await join('New hidden meeting', 550, 1100, id);
 await recording('New hidden meeting');
 const nextHidden = await poll(async () => (await call()).notifications.recording, 'new call auto-records without visible window');
 assert.ok(nextHidden.body.includes('New hidden meeting'));
 await call({action: 'notification-stop'});
 await poll(async () => !(await call()).statuses.some(s => s.recording), 'new hidden meeting stopped');
 await call({action: 'reopen'});
 await automatic(false);
 for (const pair of pairs) {await pair.meeting.evaluate(() => meeting.pc.close()); await pair.remote.evaluate(() => meeting.pc.close());}
 await poll(async () => !(await call()).meetings.length, 'inventory removes ended calls');
 // Release completed fixtures and their Web Audio graphs before the next scenario.
 for (const pair of pairs) {await pair.meeting.close(); await pair.remote.close();}
 pairs.length = 0;
 evidence.scenarios.push({name: 'auto-record-hidden-native-notification-stop-all-new-call-reopen'});
 // D30: the real app must not auto-record connected pre-join receive slots.
 const {connectReceiveSlots, switchReceiveSlot} = await import(process.env.CASSINI_TEST_TOPOLOGIES || path.join(root, 'browser-protocol/scenarios/product-topologies.mjs'));
 const prejoin = await context.newPage(), prejoinRemote = await remoteBrowser.newPage();
 for (const page of [prejoin, prejoinRemote]) await page.goto(`http://127.0.0.1:${server.address().port}/`);
 await prejoin.evaluate(() => {document.title = 'Pre-join evidence fixture';});
 await automatic(true);
 const previousFolders = (await Promise.all([artifacts, path.join(artifacts, '.cassini', 'sessions')].map(async parent => {
  let entries;
  try {entries = await readdir(parent, {withFileTypes: true});}
  catch (error) {if (error.code === 'ENOENT') return []; throw error;}
  return entries.filter(entry => entry.isDirectory() && entry.name.startsWith('Recording-')).map(entry => path.join(parent, entry.name));
 }))).flat().sort();
 await poll(async () => JSON.stringify((await call()).library.map(m => m.path).sort()) === JSON.stringify(previousFolders), 'saved Library observed before pre-join');
 await connectReceiveSlots(prejoin, prejoinRemote, [[1100], [1320], [1540]], {prejoin: true});
 await prejoin.evaluate(() => {
  window.prejoinEvidenceSamples = [];
  window.prejoinEvidenceClock = setInterval(async () => {
   const stats = await topologyPeer.getStats();
   prejoinEvidenceSamples.push([...stats.values()].filter(r => r.type === 'inbound-rtp' && r.kind === 'audio').map(r => r.packetsReceived));
  }, 1000);
 });
 const candidate = await poll(async () => (await call()).meetings.find(m => m.title === 'Pre-join evidence fixture'), 'pre-join is listed');
 assert.equal(candidate.sourceCount, 3, 'preview sender is not counted');
 assert.equal(candidate.heard, false);
 await poll(async () => {
  const state = await call();
  assert.ok(!state.statuses.some(s => s.recording), 'auto-record started before receiving call audio');
  assert.deepEqual(state.library.map(m => m.path).sort(), previousFolders, 'pre-join created a Library folder');
  return (await prejoin.evaluate(() => prejoinEvidenceSamples.length)) >= 3;
 }, 'three actual pre-join stats samples without recording');
 const beforeJoin = await prejoin.evaluate(() => prejoinEvidenceSamples);
 assert.ok(beforeJoin.every(sample => sample.length === 0), 'pre-join has no inbound RTP report');
 await switchReceiveSlot(prejoinRemote, 0, [1100]);
 const joinedDirectory = await recording('Pre-join evidence fixture');
 const heard = await poll(async () => (await call()).meetings.find(m => m.title === 'Pre-join evidence fixture' && m.heard === true), 'joined call evidence reaches Library');
 assert.equal(heard.sourceCount, 3);
 const joinedAudio = await samples(joinedDirectory);
 assert.ok(joinedAudio.tracks.some(t => t.kind === 'remote' && power(t, 1100) > .001), 'joined remote audio recorded');
 await call({action: 'stop', path: joinedDirectory});
 await poll(async () => (await samples(joinedDirectory)).manifest.state === 'completed', 'joined evidence fixture saved');
 await automatic(false);
 await prejoin.close(); await prejoinRemote.close();
 await poll(async () => !(await call()).meetings.length, 'pre-join scenario cleaned up');
 evidence.scenarios.push({name: 'prejoin-receive-slots-do-not-auto-record-until-inbound-rtp', beforeJoin,
  sourceCount: candidate.sourceCount, heardBefore: candidate.heard, heardAfter: heard.heard, directory: joinedDirectory});

 await (await import('./source-changes.mjs')).run({context, call, join, record, recording, samples, power, poll, automatic, id, artifacts, evidence});
 console.log('PASS real Library Record/individual Stop/global Stop; concurrent muted source capture and mic copies; windowless auto-record/native notification controls');
} catch (error) {
 evidence.failure = error.stack;
 try {evidence.failureState = await call();} catch {}
 evidence.peerStates = [];
 for (const pair of pairs) for (const page of [pair.meeting, pair.remote]) {
  try {evidence.peerStates.push(await page.evaluate(() => ({
   connection: window.meeting?.pc?.connectionState, ice: window.meeting?.pc?.iceConnectionState,
   gathering: window.meeting?.pc?.iceGatheringState,
   receivers: window.meeting?.pc?.getReceivers().map(receiver => ({kind: receiver.track?.kind, state: receiver.track?.readyState})),
   hookWrapped: RTCPeerConnection.prototype.createOffer.toString().includes('remember'),
   discovery: window.cassiniDiscoveryEvidence
  })));} catch {}
 }
 throw error;
}
finally {
 if (ui && ui.exitCode === null) {
  try {await call({action: 'quit'}); ui.stdin.end(); await Promise.race([new Promise(resolve => ui.once('exit', resolve)), new Promise(resolve => setTimeout(resolve, 2000))]);} catch {}
  if (ui.exitCode === null) ui.kill();
 }
 microphone?.kill();
 await context?.close(); await remoteBrowser?.close(); server.close();
 await writeFile(path.join(artifacts, 'app-control-results.json'), JSON.stringify(evidence, null, 2));
 await rm(profile, {recursive: true, force: true});
}
