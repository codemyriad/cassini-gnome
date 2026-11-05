// Shared test 2 (D68): the shared live-folder driver (browser-protocol/scenarios/live-folder.mjs)
// against GNOME's real app, production native hosts and the vendored extension, on private
// devices only. The folder is judged by the vendored oracle (conformance/live-folder/check.py),
// run by the driver after Stop and before the app quits.
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {execFileSync, spawn} from 'node:child_process';
import {createServer} from 'node:http';
import {createConnection} from 'node:net';
import {createWriteStream} from 'node:fs';
import {copyFile, mkdtemp, mkdir, readFile, readdir, writeFile, rm} from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {createInterface} from 'node:readline';
import {chromium} from 'playwright';
assert.equal(process.env.CASSINI_ISOLATED_AUDIO, '1', 'Use private recording harness');
assert.ok(!process.env.WAYLAND_DISPLAY);
assert.match(process.env.PULSE_SERVER || '', /^unix:\/tmp\/cassini-audio-/);
const root = path.resolve(import.meta.dirname, '../..');
const protocol = path.join(root, 'browser-protocol');
const {runLiveFolder} = await import(path.join(protocol, 'scenarios/live-folder.mjs'));
const {livePids, stopLaunchers, stopRelays, writeNativeHost} = await import(path.join(protocol, 'scenarios/native-launcher.mjs'));
const {poll} = await import(path.join(protocol, 'scenarios/folder-clock.mjs'));
const artifacts = process.env.CASSINI_TEST_ARTIFACTS;
const library = path.join(artifacts, 'Library');
process.env.CASSINI_LIBRARY_DIR = library;
const extension = process.env.CASSINI_TEST_EXTENSION || path.join(root, 'browser-extension');
const build = process.env.CASSINI_TEST_BUILD || path.join(root, '_build');
const profile = await mkdtemp(path.join(os.tmpdir(), 'cassini-live-folder-'));
const fixture = await readFile(path.join(root, 'tests/recording/fixture.html'));
const server = createServer((_req, res) => {res.setHeader('Content-Type', 'text/html'); res.end(fixture);});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const TITLE = 'Shared live folder meeting', MICROPHONE_HZ = 660;
const evidence = {console: [], log: []}, pending = [];
let context, remoteBrowser, ui, microphone, broker, controlHost, registered = {}, cleaning = null;
const log = line => {evidence.log.push(line); console.log(line);};
// Failure evidence: the meeting and remote pages' consoles (wall ms, comparable with
// evidence.json timelineOriginMs) go to a file as they arrive, so a hard bound keeps them.
const pageLog = createWriteStream(path.join(artifacts, 'pages-console.log'), {flags: 'a'});
// Host and cgroup pressure each second (a sampling cadence, not a wait): seconds-long whole
// machine stalls (memory reclaim refaulting binaries from a busy disk) stall every audio path
// at once, and this tells such a run from a product fault.
const pressureLog = createWriteStream(path.join(artifacts, 'pressure.log'), {flags: 'a'});
const pressureFiles = ['/proc/pressure/cpu', '/proc/pressure/io', '/proc/pressure/memory', '/sys/fs/cgroup/io.pressure', '/sys/fs/cgroup/memory.pressure'];
const pressure = setInterval(async () => {
 const values = await Promise.all(pressureFiles.map(file => readFile(file, 'utf8').then(text => text.trim().replace(/\n/g, ' | '), () => '-')));
 pressureLog.write(`${Date.now()} ${pressureFiles.map((file, i) => `${file}: ${values[i]}`).join(' ; ')}\n`);
}, 1000);
pressure.unref();
function watchPage(page, name) {
 page.on('console', message => pageLog.write(`${Date.now()} ${name} ${message.type()} ${message.text().slice(0, 4000)}\n`));
 page.on('pageerror', error => pageLog.write(`${Date.now()} ${name} pageerror ${String(error?.stack || error).slice(0, 4000)}\n`));
 page.on('crash', () => pageLog.write(`${Date.now()} ${name} crash\n`));
}
// Passive, main world: the extension's page<->relay control traffic (track, trackEnd,
// sources, errors, warnings, stops, failed acks) and every 2 s per track the newest audio
// chunk's offsetMs against wall time, so a stalled or drifting capture clock is visible.
function observeExtension() {
 const latest = new Map();
 const say = value => console.log('cassini-msg ' + JSON.stringify(value).slice(0, 3000));
 window.addEventListener('message', event => {
  const data = event.data;
  if (event.source !== window || !data || !['cassini-page', 'cassini-control'].includes(data.channel)) return;
  const now = Date.now();
  if (data.type === 'packet' && data.packet?.type === 'audio') {
   const {id, sequence, offsetMs} = data.packet, seen = latest.get(id);
   if (!seen || now - seen >= 2000) {latest.set(id, now); say({now, audio: id, sequence, offsetMs: Math.round(offsetMs * 10) / 10});}
   return;
  }
  if (data.channel === 'cassini-control' && ((data.type === 'ack' && !data.error) || data.type === 'discover')) return;
  const {moduleURL, workletURL, packet, ...rest} = data;
  say({now, ...rest, ...(packet ? {packet: {...packet, pcm: undefined}} : {})});
 });
}
function call(packet={action: 'status'}) {
 return new Promise((resolve, reject) => {
  const timer = setTimeout(() => reject(new Error('GNOME UI request timed out')), 12000);
  pending.push(value => {clearTimeout(timer); value.error != null ? reject(new Error(value.error || 'GNOME UI request failed')) : resolve(value);});
  ui.stdin.write(JSON.stringify(packet) + '\n');
 });
}
const active = async () => (await call()).statuses.find(s => s.recording && s.title === TITLE && s.path);
// The browser process of this run's persistent context: the one Chromium process with this
// profile and no --type (renderers, GPU and utility processes carry one). Chromium rewrites
// its cmdline as one space-separated title, so split on both (the profile has no space).
async function browserPid() {
 const found = [];
 for (const entry of await readdir('/proc')) {
  if (!/^\d+$/.test(entry)) continue;
  let argv;
  try {argv = (await readFile(`/proc/${entry}/cmdline`, 'utf8')).split(/[\0 ]/);}
  catch (error) {if (['ENOENT', 'ESRCH', 'EACCES'].includes(error.code)) continue; throw error;}
  if (argv.includes(`--user-data-dir=${profile}`) && !argv.some(arg => arg.startsWith('--type='))) found.push(Number(entry));
 }
 assert.equal(found.length, 1, `one browser process for ${profile}, found ${JSON.stringify(found)}`);
 return found[0];
}
// A child's exit, or false once ms pass (a bound on that process's own exit, not a wait).
const exitWithin = (child, ms) => child.exitCode !== null || child.signalCode !== null ? Promise.resolve(true)
 : new Promise(resolve => {const timer = setTimeout(() => resolve(false), ms); child.once('exit', () => {clearTimeout(timer); resolve(true);});});
// The driver's adapter cleanup() (and this script's own finally): stop the app's recording,
// quit the app, close both browsers, end the relays, any control launcher left waiting on
// its FIFO, the broker, the microphone producer and any host still alive. Idempotent (one
// run, whoever asks first); bounded per step; evidence.cleanup says what each step did.
// The control-pipe logs stay in the profile for the finally below to copy.
function cleanup({failed = false} = {}) {
 return cleaning ??= (async () => {
  const done = evidence.cleanup = {failed};
  const step = async (name, run) => {try {done[name] = (await run()) ?? 'done';} catch (error) {done[name] = String(error?.stack || error);}};
  if (ui && ui.exitCode === null && ui.signalCode === null) {
   if (failed) await step('failureState', async () => {evidence.failureState = await call();});
   await step('stopRecording', async () => {
    if (!(await call()).statuses.some(s => s.recording)) return 'none';
    await call({action: 'stop-all'});
    await poll(async () => !(await call()).statuses.some(s => s.recording), 'cleanup: the app stops its recording', 15000);
   });
   await step('quit', async () => {
    const quit = exitWithin(ui, 10000); // bound on the app's exit
    await call({action: 'quit'}); ui.stdin.end();
    if (!await quit) {ui.kill('SIGKILL'); return 'killed';}
   });
  }
  if (microphone) await step('microphone', async () => {
   const exited = exitWithin(microphone, 5000); // bound on the producer's exit
   microphone.kill();
   if (!await exited) {microphone.kill('SIGKILL'); return 'killed';}
  });
  // Closing the browser closes each host's stdin; the host exits and its relay sees the end
  // of its input. Bounded: a browser that does not close is killed.
  for (const [name, close] of [['context', () => context?.close()], ['remoteBrowser', () => remoteBrowser?.close()]])
   await step(name, () => Promise.race([close(), new Promise((_, reject) => setTimeout(() => reject(new Error(`${name} did not close in 20 s`)), 20000).unref())]));
  await step('browserKilled', async () => {
   // The persistent context's browser, if close() left it running.
   try {const pid = await browserPid(); process.kill(pid, 'SIGKILL'); return pid;} catch {return 'none';}
  });
  server.close();
  if (controlHost) {
   await step('relaysStopped', () => stopRelays(controlHost));
   await step('launchersKilled', () => stopLaunchers(controlHost));
  }
  if (broker && broker.exitCode === null && broker.signalCode === null) await step('broker', async () => {
   const exited = exitWithin(broker, 10000); // bound on the broker's exit
   broker.kill('SIGTERM');
   if (!await exited) {broker.kill('SIGKILL'); return 'killed';}
  });
  // Killed by us (signal SIGTERM) means it ran, so held the serve lock, for the whole run.
  if (broker) evidence.broker = {exitCode: broker.exitCode, signal: broker.signalCode};
  // Backstop: no production host outlives the run (the browser's exit normally ends them).
  await step('hostsKilled', async () => {
   const killed = [];
   for (const host of Object.values(registered)) for (const pid of await livePids(host)) {
    try {process.kill(pid, 'SIGKILL'); killed.push(pid);} catch (error) {if (error.code !== 'ESRCH') throw error;}
   }
   return killed;
  });
  return done;
 })();
}
async function recordingFolders() {
 const names = new Set();
 for (const parent of [library, path.join(library, '.cassini', 'sessions')]) {
  let entries;
  try {entries = await readdir(parent, {withFileTypes: true});}
  catch (error) {if (error.code === 'ENOENT') continue; throw error;}
  for (const entry of entries) if (entry.isDirectory() && entry.name.startsWith('Recording-')) names.add(entry.name);
 }
 return [...names].sort();
}
try {
 const manifest = JSON.parse(await readFile(path.join(extension, 'manifest.json')));
 const id = [...createHash('sha256').update(Buffer.from(manifest.key, 'base64')).digest('hex').slice(0, 32)].map(c => String.fromCharCode(97 + parseInt(c, 16))).join('');
 assert.equal(id, 'pcafdaahfhjiolfpfnlhmljfgoabbaij');
 await mkdir(library, {recursive: true});
 // The production hosts, each behind the shared pid-recording launcher.
 const hosts = path.join(profile, 'hosts'), pids = path.join(profile, 'pids');
 await mkdir(hosts, {recursive: true});
 const shims = {
  'io.codemyriad.cassini': `import sys\nsys.path.insert(0,${JSON.stringify(root + '/src')})\nsys.stderr=open(${JSON.stringify(path.join(artifacts, 'audio-host.log'))},'a',buffering=1)\nfrom cassini_talk.browser_host import main\nsys.exit(main(directory=${JSON.stringify(library)},open_completed=False))\n`,
  'io.codemyriad.cassini.control': `import sys\nsys.path.insert(0,${JSON.stringify(root + '/src')})\nsys.stderr=open(${JSON.stringify(path.join(artifacts, 'control-host.log'))},'a',buffering=1)\nfrom cassini_talk.browser_control_host import main\nsys.exit(main())\n`,
 };
 for (const [name, script] of Object.entries(shims)) {
  const file = path.join(hosts, name + '.py');
  await writeFile(file, script, {mode: 0o600});
  // The control host goes through the observing launcher: the driver reads every resume
  // request off its stdout (browser-protocol scenario.json resumeObservation).
  registered[name] = await writeNativeHost({manifestDirectory: path.join(profile, 'NativeMessagingHosts'), launcherDirectory: hosts,
   pidDirectory: pids, name, command: ['/usr/bin/python3', file], allowedOrigins: [`chrome-extension://${id}/`], description: 'Isolated app test',
   ...(name === 'io.codemyriad.cassini.control' ? {observe: path.join(profile, 'control-pipe')} : {})});
 }
 controlHost = registered['io.codemyriad.cassini.control'];
 // The production broker, started as ensure_service would (same module and socket), but
 // with its log kept: ensure_service discards its stderr. Hosts and app connect to it.
 const service = execFileSync('/usr/bin/python3', ['-I', '-c', `import sys\nsys.path.insert(0,${JSON.stringify(root + '/src')})\nfrom cassini_talk.browser_service import service_path\nprint(service_path())`], {encoding: 'utf8'}).trim();
 broker = spawn('/usr/bin/python3', ['-c', `import logging,sys\nlogging.basicConfig(level=logging.INFO,format='%(created).3f %(levelname)s %(name)s %(threadName)s: %(message)s')\nsys.argv=['browser_service','--serve',${JSON.stringify(service)}]\nlogging.getLogger('live-folder').info('broker starting on %s',sys.argv[2])\nfrom cassini_talk.browser_service import main\nmain()\nlogging.getLogger('live-folder').warning('broker main returned (lock held elsewhere, or closed)')\n`],
  {env: {...process.env, PYTHONPATH: path.join(root, 'src')}, stdio: ['ignore', 'ignore', 'pipe'], detached: true});
 broker.stderr.pipe(createWriteStream(path.join(artifacts, 'broker.log')));
 await poll(() => new Promise(resolve => {
  const socket = createConnection(service).once('connect', () => {socket.destroy(); resolve(true);}).once('error', () => resolve(false));
 }), 'the browser broker accepts connections', 10000);
 ui = spawn('/usr/bin/python3', [path.join(root, 'tests/recording/browser-ui.py'), '--build', build], {stdio: ['pipe', 'pipe', 'pipe'], env: {...process.env}});
 ui.stderr.pipe(createWriteStream(path.join(artifacts, 'gnome-ui.log')));
 createInterface({input: ui.stdout}).on('line', line => {try {pending.shift()?.(JSON.parse(line));} catch {evidence.console.push(line);}});
 ui.on('exit', code => {evidence.uiExit = code;});
 await poll(async () => (await call()).status === 'Library', 'GNOME Library window', 30000);
 microphone = spawn('/usr/bin/python3', ['-c', `import gi,signal\ngi.require_version('Gst','1.0')\nfrom gi.repository import Gst,GLib\nGst.init(None)\np=Gst.parse_launch('audiotestsrc is-live=true freq=${MICROPHONE_HZ} volume=0.2 ! audioconvert ! audioresample ! audio/x-raw,rate=48000,channels=1 ! pulsesink device=cassini_microphone sync=false')\nloop=GLib.MainLoop()\nsignal.signal(signal.SIGTERM,lambda *_:loop.quit())\np.set_state(Gst.State.PLAYING)\ntry: loop.run()\nfinally:p.set_state(Gst.State.NULL)\n`], {stdio: ['ignore', 'ignore', 'pipe']});
 microphone.stderr.pipe(createWriteStream(path.join(artifacts, 'microphone-producer.log')));
 const flags = ['--no-sandbox', '--ozone-platform=x11', '--autoplay-policy=no-user-gesture-required', '--allow-loopback-in-peer-connection', '--disable-features=WebRtcHideLocalIpsWithMdns'];
 context = await chromium.launchPersistentContext(profile, {channel: 'chromium', headless: false, ignoreDefaultArgs: ['--mute-audio'], args: [`--disable-extensions-except=${extension}`, `--load-extension=${extension}`, ...flags]});
 const worker = context.serviceWorkers()[0] || await context.waitForEvent('serviceworker');
 assert.equal(new URL(worker.url()).host, id);
 worker.on('console', message => evidence.console.push(message.text()));
 await poll(async () => (await call()).connected > 0, 'actual companion handshake in Library', 20000);
 remoteBrowser = await chromium.launch({headless: false, ignoreDefaultArgs: ['--mute-audio'], args: flags});
 const meeting = await context.newPage(), remote = await remoteBrowser.newPage();
 watchPage(meeting, 'meeting'); watchPage(remote, 'remote');
 await meeting.addInitScript(observeExtension);
 for (const page of [meeting, remote]) await page.goto(`http://127.0.0.1:${server.address().port}/`);
 await meeting.evaluate(title => {document.title = title;}, TITLE);
 let folder = null;
 const app = {
  name: 'gnome', nativeTones: {microphone: MICROPHONE_HZ},
  protocolCommit: (await readFile(path.join(protocol, 'PINNED_COMMIT'), 'utf8')).trim(),
  async record() {
   await poll(async () => (await call()).recordReady, 'nothing else is saving before Record', 20000);
   await call({action: 'record'}); // the real Library Record action
  },
  async snapshot() {
   const status = await active();
   if (status) folder = status.path;
   return status ? {path: status.path, captureId: status.captureId} : null;
  },
  async meetingRow() {
   const row = (await call()).meetings.find(m => m.title === TITLE);
   return row ? {sourceCount: row.sourceCount, heard: row.heard} : null;
  },
  async selectMe(on) {
   // The real What is recorded dialog: choose, wait until the app applied it, close.
   await call({action: 'selection', path: folder, me: on, others: true});
   await poll(async () => {
    const status = await call();
    const chosen = status.statuses.find(s => s.path === folder && s.recording)?.events?.filter(e => e.type === 'selection').at(-1);
    return !status.sourceChangePending && chosen?.me === on && chosen?.others === true;
   }, `the source dialog applies me:${on}`, 12000);
   await call({action: 'sources-done'});
  },
  async stop() {await call({action: 'stop', path: folder});},
  async saved(directory) {
   // GNOME also writes the app-owned end facts after Stop.
   await poll(async () => {
    try {return JSON.parse(await readFile(path.join(directory, 'meeting.json'))).recordingDetails?.end?.reason === 'user';}
    catch (error) {if (['ENOENT'].includes(error.code) || error instanceof SyntaxError) return false; throw error;}
   }, 'Stop saves meeting.json recordingDetails.end', 30000);
  },
  audioHostPids: () => livePids(registered['io.codemyriad.cassini']),
  folders: recordingFolders,
  controlHost, browserPid, cleanup,
 };
 const started = Date.now();
 const outcome = await runLiveFolder({meeting, remote, app, worker, out: artifacts, log, python: '/usr/bin/python3'});
 evidence.scenarioMs = Date.now() - started;
 evidence.outcome = {code: outcome.code, folder: outcome.folder, pass: outcome.result?.pass,
  failed: outcome.result?.checks?.flatMap(c => c.failed)};
 assert.equal(outcome.code, 0, `check.py failed (exit ${outcome.code}):\n${outcome.stdout}${outcome.stderr}`);
 log(`PASS shared live folder (D68): check.py passed in ${evidence.scenarioMs} ms of scenario`);
} catch (error) {
 evidence.failure = error.stack;
 // A failure before the driver's teardown: the app's state now (the driver's cleanup takes it
 // itself before quitting the app).
 if (!cleaning && ui && ui.exitCode === null) try {evidence.failureState = await call();} catch {}
 await cleanup({failed: true});
 throw error;
}
finally {
 await cleanup();
 // The relays' logs are evidence; their FIFOs stay in the private profile, which goes.
 if (controlHost) try {
  for (const connection of await readdir(controlHost.observe)) for (const file of ['host', 'frames.jsonl', 'relay.err']) {
   await mkdir(path.join(artifacts, 'control-pipe', connection), {recursive: true});
   await copyFile(path.join(controlHost.observe, connection, file), path.join(artifacts, 'control-pipe', connection, file)).catch(error => {if (error.code !== 'ENOENT') throw error;});
  }
 } catch (error) {evidence.controlPipeCopy = String(error);}
 clearInterval(pressure);
 await new Promise(resolve => pageLog.end(resolve));
 await new Promise(resolve => pressureLog.end(resolve));
 await writeFile(path.join(artifacts, 'live-folder-results.json'), JSON.stringify(evidence, null, 2));
 await rm(profile, {recursive: true, force: true});
}
