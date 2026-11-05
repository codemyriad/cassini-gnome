// Unit tests for the live-folder driver's pieces that need no browser (shared test 2, D68):
// the folder clock against make.py's synthetic folder, its tolerant reads and bounded
// waits, the pid-recording native host launcher, the scenario constants the driver uses,
// and runLiveFolder's teardown on failure (fake pages and app, real launcher and relay).
import assert from 'node:assert/strict';
import { execFileSync, spawn } from 'node:child_process';
import { closeSync, constants, openSync } from 'node:fs';
import { mkdtemp, open, readdir, readFile, rm, stat, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { eventsOf, extent, F, folderClock, S } from '../scenarios/folder-clock.mjs';
import { controlConnections, hostPids, livePids, stopLaunchers, stopRelays, writeNativeHost } from '../scenarios/native-launcher.mjs';
import * as driver from '../scenarios/live-folder.mjs';
const { loadScenario, runLiveFolder } = driver;
import { poll } from '../scenarios/folder-clock.mjs';

const root = path.resolve(import.meta.dirname, '..');
const scratch = await mkdtemp(path.join(os.tmpdir(), 'live-folder-driver-'));
// A failing test must not leave launchers, hosts or relays behind.
const children = new Set(), hosts = [];
test.after(async () => {
  for (const child of children) {
    child.stdin?.destroy(); child.stdout?.destroy();
    if (child.exitCode === null && child.signalCode === null) child.kill('SIGKILL');
  }
  for (const host of hosts) await stopRelays(host);
  await rm(scratch, { recursive: true, force: true });
});
const BOUND = 10000;  // bound on each wait below; every wait ends on its event

async function synthetic() {
  const out = path.join(scratch, `make-${Math.random().toString(36).slice(2)}`);
  execFileSync('python3', [path.join(root, 'conformance/live-folder/make.py'), out]);
  const [folder] = (await readdir(out)).filter(name => name.startsWith('Recording-'));
  return path.join(out, folder);
}

test('the folder clock reads make.py\'s synthetic run as the oracle places it', async () => {
  const folder = await synthetic();
  const clock = folderClock(folder);
  const manifest = await clock.read();
  const end = 109625;  // make.py END_MS: every live track ends at Stop
  assert.equal(S(manifest), end);
  assert.equal(clock.S(), end);
  assert.equal(F(manifest, 'doc-1:source:1'), end);         // A: span-a2 runs to Stop
  assert.equal(F(manifest, 'doc-1:source:2'), end);         // B
  assert.equal(F(manifest, { kind: 'system' }), 99500);     // the fallback ends at restored
  assert.equal(F(manifest, t => t.id === 'span-a1'), 95350.0625);
  assert.equal(F(manifest, 'no-such-source'), -Infinity);
  assert.deepEqual(extent(manifest.tracks.find(t => t.id === 'microphone-1')), [0, 2100]);
  assert.deepEqual(eventsOf(manifest, 'othersSource').map(e => [e.source, e.reason]),
    [['browser', 'start'], ['system', 'lost'], ['browser', 'restored']]);
  assert.equal(await clock.waitFolder(m => m.state === 'completed' && 'done', 'synthetic folder is complete', 1000), 'done');
});

test('a partial or missing session.json keeps the last good manifest', async () => {
  const folder = await synthetic();
  const clock = folderClock(path.join(folder, 'not-yet'));
  assert.equal(await clock.read(), null);
  assert.equal(clock.S(), -Infinity);
  const good = folderClock(folder);
  const manifest = await good.read();
  const text = await readFile(path.join(folder, 'session.json'), 'utf8');
  await writeFile(path.join(folder, 'session.json'), text.slice(0, text.length >> 1));
  assert.equal(await good.read(), manifest);
  assert.equal(good.rejected, 1);
  await writeFile(path.join(folder, 'session.json'), '{"tracks": 3}');
  assert.equal(await good.read(), manifest);
  await writeFile(path.join(folder, 'session.json'), text);
  assert.notEqual(await good.read(), manifest);
  assert.equal(good.S(), 109625);
});

test('waitFolder fails with its label when the bound passes, and at once when pred throws', async () => {
  const clock = folderClock(await synthetic(), { pollMs: 5 });
  await assert.rejects(clock.waitFolder(() => clock.S() > 200000, 'never: S beyond the run', 50),
    /Timed out after 50 ms: never: S beyond the run \(\{"S":109625/);
  await assert.rejects(clock.waitFolder(() => { throw new Error('invariant broken'); }, 'unused', 10000), /invariant broken/);
  await assert.rejects(clock.waitFolder(() => true, 'no bound', 0), RangeError);
});

test('the launcher records the host\'s own pid, passes the browser\'s arguments and execs', async () => {
  const directory = path.join(scratch, 'hosts');
  const command = [process.execPath, '-e', 'process.stdout.write(JSON.stringify([process.pid, process.argv.slice(1)])); process.stdin.resume()'];
  const host = await writeNativeHost({ manifestDirectory: path.join(directory, 'NativeMessagingHosts'),
    launcherDirectory: path.join(directory, "it's here"), pidDirectory: path.join(directory, 'pids'),
    name: 'io.example.audio', command, allowedOrigins: ['chrome-extension://abc/'] });
  const manifest = JSON.parse(await readFile(host.manifest, 'utf8'));
  assert.equal(manifest.path, host.launcher);
  assert.deepEqual(manifest.allowed_origins, ['chrome-extension://abc/']);
  await writeNativeHost({ manifestDirectory: path.join(directory, 'NativeMessagingHosts'), pidDirectory: path.join(directory, 'pids'),
    name: 'io.example.audio.control', command, allowedOrigins: [] });
  const child = spawn(host.launcher, ['chrome-extension://abc/', '--parent-window=0'], { stdio: ['pipe', 'pipe', 'inherit'] });
  const [pid, argv] = await new Promise((resolve, reject) => {
    child.stdout.once('data', data => resolve(JSON.parse(data))); child.once('error', reject);
  });
  assert.equal(pid, child.pid, 'exec keeps the launcher\'s pid');
  assert.deepEqual(argv, ['chrome-extension://abc/', '--parent-window=0']);
  assert.deepEqual(await livePids(host), [child.pid]);
  assert.deepEqual(await livePids({ ...host, name: 'io.example.audio.control' }), [], 'a longer host name is another host');
  const exited = new Promise(resolve => child.once('exit', resolve));
  process.kill(child.pid, 'SIGKILL');
  await exited;
  assert.deepEqual(await hostPids(host), [{ pid: child.pid, live: false }]);
  assert.deepEqual(await livePids(host), []);
  await assert.rejects(writeNativeHost({ ...host, manifestDirectory: directory, name: 'Bad Name', allowedOrigins: [] }), /invalid native host name/);
});

test('the driver reads scenario.json and the corpus constants it names', async () => {
  const { constants, bounds, pollMs } = await loadScenario();
  const corpus = JSON.parse(await readFile(path.join(root, 'conformance/multistream/corpus.json'), 'utf8')).constants;
  for (const name of ['LATE_REMOTE_MS', 'BLEED_DB', 'SPAN_RATIO_MIN', 'SPARSE_DENSITY_MAX']) assert.equal(constants[name], corpus[name]);
  assert.equal(pollMs, 100);
  assert.equal(bounds.restore, constants.RESUME_BOUND_MS);
  for (const name of ['setup', 'record', 'meOff', 'muteHold', 'unmute', 'lateRemote', 'meOn', 'lost', 'restore', 'stop'])
    assert.ok(bounds[name] > 0, name);
});

// runLiveFolder with fake pages and a fake app around a real observing launcher and relay.
// The test process plays the browser: it starts the control launcher itself.
const exitOf = child => new Promise(resolve => child.exitCode !== null || child.signalCode !== null
  ? resolve({ code: child.exitCode, signal: child.signalCode }) : child.once('exit', (code, signal) => resolve({ code, signal })));
// Gone, or a zombie no one reaps (an orphaned relay's new parent may be a container's pid 1).
async function ended(pid) {
  try { process.kill(pid, 0); } catch { return true; }
  try { const text = await readFile(`/proc/${pid}/stat`, 'utf8'); return text.slice(text.lastIndexOf(')') + 2).startsWith('Z'); }
  catch { return process.platform === 'linux'; }
}
const HELLO = `const b=Buffer.from('{"type":"hello"}'),h=Buffer.alloc(4);h['writeUInt32'+require('os').endianness()](b.length);
process.stdout.on('error',()=>{});process.stdout.write(Buffer.concat([h,b]));process.stdin.resume();process.stdin.on('end',()=>process.exit(0));`;
const page = { evaluate: async () => ({}), waitForFunction: async () => {}, url: () => 'http://meeting.test/' };
const worker = origin => ({
  async evaluate(fn) {
    const source = String(fn);
    if (source.includes('getManifest')) return '0.0.0-test';
    if (source.includes('tabs.query')) return [7];
    if (source.includes('cassiniSessions')) return [{ tabId: 7, captureId: 'capture-test', startedAt: origin }];
    throw new Error(`unexpected service worker call ${source}`);
  },
});
async function controlRun(name, { node } = {}) {
  const directory = path.join(scratch, name);
  const host = await writeNativeHost({ manifestDirectory: path.join(directory, 'NativeMessagingHosts'), pidDirectory: path.join(directory, 'pids'),
    observe: path.join(directory, 'control pipe'), name: 'io.example.control', command: [process.execPath, '-e', HELLO],
    allowedOrigins: [], ...(node ? { node } : {}) });
  hosts.push(host);
  const browser = spawn(host.launcher, ['chrome-extension://abc/'], { stdio: ['pipe', 'pipe', 'inherit'] });
  children.add(browser);
  browser.stdout.resume();
  const out = path.join(directory, 'out');
  execFileSync('mkdir', ['-p', out]);
  return { host, browser, out };
}
function fakeApp({ host, browser }, overrides) {
  const calls = { record: 0, stop: 0, cleanup: [] };
  const app = {
    name: 'synthetic', protocolCommit: '0'.repeat(40), nativeTones: { microphone: 660 }, controlHost: host,
    browserPid: async () => process.pid, meetingRow: async () => ({ sourceCount: 3, heard: true }), folders: async () => [],
    audioHostPids: async () => [], snapshot: async () => null,
    async record() { calls.record++; }, async stop() { calls.stop++; }, async selectMe() {},
    // The browser closes: the host's stdin ends (a launcher blocked in open() never reads it).
    async cleanup(options) { calls.cleanup.push(options); browser.stdin.end(); browser.stdout.destroy(); },
    ...overrides,
  };
  return { app, calls };
}

test('a failure after Record stops the recording, runs cleanup once and stops the relays', async () => {
  const run = await controlRun('after-record');
  const [connection] = await poll(async () => { const c = await controlConnections(run.host); return c[0]?.relay && c[0].frames.length ? c : null; },
    'the relay starts and logs the hello', BOUND, { pollMs: 5 });
  // Something else keeps the FIFO open for writing, so the relay's input never ends when the
  // browser closes: only stopRelays ends it.
  const holder = await open(path.join(run.host.observe, '1', 'out'), 'w');
  const folder = await synthetic();
  const { app, calls } = fakeApp(run, {
    snapshot: async () => (calls.record ? { path: folder, captureId: 'capture-test' } : null),
    async selectMe() { throw new Error('injected failure after Record'); },
  });
  const logged = [];
  await assert.rejects(runLiveFolder({ meeting: page, remote: page, app, worker: worker(Date.now()), out: run.out, log: line => logged.push(line) }),
    /injected failure after Record/);
  assert.deepEqual([calls.record, calls.stop, calls.cleanup], [1, 1, [{ failed: true }]], 'Record asked, so Stop asked; cleanup once');
  assert.equal(await exitOf(run.browser).then(r => r.code), 0, 'the host saw its stdin end and exited');
  await poll(() => ended(connection.relay), 'the relay is gone', BOUND, { pollMs: 5 });
  const teardown = JSON.parse(await readFile(path.join(run.out, 'teardown.json'), 'utf8'));
  assert.deepEqual(teardown, { failed: true, stop: 'done', cleanup: 'done', relaysStopped: [connection.relay], launchersKilled: [] });
  const partial = JSON.parse(await readFile(path.join(run.out, 'evidence.partial.json'), 'utf8'));
  assert.match(partial.notes.failure, /injected failure after Record/);
  assert.equal(partial.captureId, 'capture-test');
  await holder.close();
});

test('a failure during the control launcher\'s startup runs cleanup once and kills the launcher waiting on its FIFO', async () => {
  // The relay's interpreter fails at once, so its FIFO is never opened for reading: the
  // launcher waits in open() and reads nothing, so closing the browser does not end it.
  const run = await controlRun('startup', { node: '/bin/false' });
  const [pending] = await poll(async () => { const c = await controlConnections(run.host); return c[0]?.pid ? c : null; },
    'the launcher records the connection', BOUND, { pollMs: 5 });
  assert.equal(pending.pid, run.browser.pid);
  // relayStartMs 0: the start bound has passed once the clock is past the launch.
  await poll(() => Date.now() > pending.launchedWall, 'the clock passes the launch', BOUND, { pollMs: 1 });
  const { app, calls } = fakeApp(run, { async record() { throw new Error('the run must fail at setup'); } });
  await assert.rejects(runLiveFolder({ meeting: page, remote: page, app, worker: worker(Date.now()), out: run.out, log: () => {}, relayStartMs: 0 }),
    /setup: control connection 1's relay did not start within 0 ms of the launch/);
  assert.deepEqual([calls.stop, calls.cleanup], [0, [{ failed: true }]], 'no Record asked, so no Stop; cleanup once');
  assert.deepEqual(await exitOf(run.browser), { code: null, signal: 'SIGKILL' }, 'the launcher was killed');
  const teardown = JSON.parse(await readFile(path.join(run.out, 'teardown.json'), 'utf8'));
  assert.deepEqual(teardown, { failed: true, cleanup: 'done', relaysStopped: [], launchersKilled: [run.browser.pid] });
  assert.deepEqual(await controlConnections(run.host).then(c => c.map(x => [x.relay, x.live])), [[null, false]], 'no relay ever ran');
});

test('a failing cleanup or an adapter without one never hides the run\'s own failure', async () => {
  const run = await controlRun('no-cleanup');
  const relay = await poll(async () => (await controlConnections(run.host))[0]?.relay, 'the relay starts', BOUND, { pollMs: 5 });
  const teardowns = [];
  for (const cleanup of [undefined, async () => { throw new Error('cleanup broke'); }]) {
    const { app } = fakeApp(run, { cleanup, async meetingRow() { throw new Error('injected setup failure'); } });
    const logged = [];
    await assert.rejects(runLiveFolder({ meeting: page, remote: page, app, worker: worker(Date.now()), out: run.out, log: line => logged.push(line) }),
      /injected setup failure/);
    assert.match(logged.join('\n'), cleanup ? /teardown: cleanup: cleanup broke/ : /teardown: cleanup: the adapter has no cleanup\(\)/);
    teardowns.push(JSON.parse(await readFile(path.join(run.out, 'teardown.json'), 'utf8')));
  }
  // With no browser close, the first teardown ended the relay itself, and the next found none.
  assert.deepEqual(teardowns.map(t => t.relaysStopped), [[relay], []]);
  assert.match(teardowns[1].cleanup.error, /cleanup broke/);
  await poll(() => ended(relay), 'the relay is gone', BOUND, { pollMs: 5 });
  run.browser.stdin.end();
  await exitOf(run.browser);
});

test('a relay that starts only after teardown killed its launcher sees the end of its input and exits', async () => {
  // The relay's interpreter starts late (a stalled machine): it waits until the test opens
  // its gate. Teardown runs before the relay has logged its start, so stopRelays cannot see
  // it, and stopLaunchers kills the launcher, the FIFO's only writer-to-be.
  const gate = path.join(scratch, 'late-gate');
  execFileSync('mkfifo', [gate]);
  const late = path.join(scratch, 'late-node.sh');
  await writeFile(late, `#!/bin/sh\nread go < '${gate}'\nexec '${process.execPath}' "$@"\n`, { mode: 0o700 });
  const run = await controlRun('late-relay', { node: late });
  // The gate opens for writing once the late interpreter waits on it (the relay is spawned).
  const held = await poll(() => {
    try { return openSync(gate, constants.O_WRONLY | constants.O_NONBLOCK); }
    catch (error) { if (error.code === 'ENXIO') return null; throw error; }
  }, 'the late relay interpreter waits on its gate', BOUND, { pollMs: 5 });
  const [before] = await controlConnections(run.host);
  assert.deepEqual([before.pid, before.relay], [run.browser.pid, null]);
  assert.deepEqual(await stopRelays(run.host), [], 'no start line yet, so no relay to stop');
  assert.deepEqual(await stopLaunchers(run.host), [run.browser.pid]);
  assert.deepEqual(await exitOf(run.browser), { code: null, signal: 'SIGKILL' });
  run.browser.stdin.end(); run.browser.stdout.destroy();  // the browser closes
  closeSync(held);  // the relay starts now
  const relay = await poll(async () => (await controlConnections(run.host))[0].relay, 'the late relay logs its start', BOUND, { pollMs: 5 });
  await poll(() => ended(relay), 'the late relay is gone (no writer will ever open its FIFO)', BOUND, { pollMs: 5 });
  const [after] = await controlConnections(run.host);
  assert.deepEqual([after.ended, after.frames.length, after.inputError, after.outputError], [true, 0, null, null]);
});

// macOS has no /proc: native-launcher.mjs takes its other branches (kill(pid, 0), ps). This
// child process runs them on Linux: process.platform darwin, /proc reads failing as there,
// and the system ps answering for the pid.
const AS_ON_MACOS = `
import { createRequire, syncBuiltinESMExports } from 'node:module';
const require = createRequire(import.meta.url);
Object.defineProperty(process, 'platform', { value: 'darwin' });
const fsp = require('node:fs/promises'), readFile = fsp.readFile;
fsp.readFile = (file, ...rest) => String(file).startsWith('/proc/')
  ? Promise.reject(Object.assign(new Error('no /proc on macOS'), { code: 'ENOENT' })) : readFile(file, ...rest);
syncBuiltinESMExports();
const { controlConnections, stopLaunchers } = await import(process.argv[1]);
const host = JSON.parse(process.argv[2]);
const [c] = await controlConnections(host);
console.log(JSON.stringify({ live: c.live, killed: await stopLaunchers(host) }));
`;

test('on macOS too, stopLaunchers kills a launcher still waiting on its FIFO', async () => {
  const run = await controlRun('macos-launcher', { node: '/bin/false' });
  await poll(async () => (await controlConnections(run.host))[0]?.pid, 'the launcher records the connection', BOUND, { pollMs: 5 });
  const child = spawn(process.execPath, ['--input-type=module', '-e', AS_ON_MACOS, path.join(root, 'scenarios/native-launcher.mjs'), JSON.stringify(run.host)],
    { stdio: ['ignore', 'pipe', 'inherit'] });
  let text = '';
  child.stdout.on('data', chunk => { text += chunk; });
  assert.deepEqual(await exitOf(child), { code: 0, signal: null });
  // Off Linux liveness is kill(pid, 0), so the waiting launcher reads as a live host; only
  // its command line (the launcher script, not the host's) tells them apart.
  assert.deepEqual(JSON.parse(text), { live: true, killed: [run.browser.pid] });
  assert.deepEqual(await exitOf(run.browser), { code: null, signal: 'SIGKILL' });
});

test('check.py is bounded: one that never exits is killed and fails the run', async () => {
  const directory = path.join(scratch, 'oracle-hang');
  execFileSync('mkdir', ['-p', directory]);
  // A python stand-in that records its pid and never exits (it waits for the end of its stdin).
  const python = path.join(directory, 'python');
  await writeFile(python, `#!/bin/sh\necho $$ > '${directory}/pid'\nexec cat\n`, { mode: 0o700 });
  assert.equal(typeof driver.runOracle, 'function', 'the driver runs check.py through runOracle');
  const started = Date.now();
  await assert.rejects(driver.runOracle({ python, folder: directory, evidence: path.join(directory, 'evidence.json'),
    result: path.join(directory, 'result.json'), ms: 300 }), /check\.py did not finish within 300 ms/);
  assert.ok(Date.now() - started < BOUND);
  const pid = Number((await readFile(path.join(directory, 'pid'), 'utf8')).trim());
  assert.ok(await ended(pid), 'the hung check.py was killed');
});
