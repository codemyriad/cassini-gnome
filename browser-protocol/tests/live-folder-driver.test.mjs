// Unit tests for the live-folder driver's pieces that need no browser (shared test 2, D68):
// the folder clock against make.py's synthetic folder, its tolerant reads and bounded
// waits, the pid-recording native host launcher, the scenario constants the driver uses,
// and runLiveFolder's teardown on failure (fake pages and app, real launcher and relay).
import assert from 'node:assert/strict';
import { execFileSync, spawn } from 'node:child_process';
import { closeSync, constants, openSync } from 'node:fs';
import { mkdtemp, open, readdir, readFile, rm, stat, symlink, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { eventsOf, extent, F, folderClock, S } from '../scenarios/folder-clock.mjs';
import { controlConnections, hostPids, livePids, processArgv, RELAY, runsCommand, stopLaunchers, stopRelays, writeNativeHost } from '../scenarios/native-launcher.mjs';
import * as driver from '../scenarios/live-folder.mjs';
import { sessionClock, wallPair } from '../scenarios/session-clock.mjs';
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

// The driver's session clock is monotonic (scenarios/session-clock.mjs): the origin is mapped onto
// it once, as SPEC requires of capturing documents. A hosted VM's wall clock ran 500 ppm fast and
// put an app's monotonic me:true stamp 38 ms before the driver's wall-clock request (Mac #148).
// The synthetic run's evidence holds the true instants. Measured through the driver's clock, every
// oracle verdict stays the same under a drifting wall clock (1000 ppm: enough on the synthetic
// run's 50 ms meOn margin) and under a 2 s step, while the old reading (Date.now() - origin)
// changes them.
test('a drifting or stepping wall clock never changes the oracle\'s verdicts', async () => {
  const folder = await synthetic(), out = path.dirname(folder);
  const base = JSON.parse(await readFile(path.join(out, 'evidence.json'), 'utf8')), origin = base.timelineOriginMs;
  const failing = async (evidence, name) => {
    const file = path.join(out, `${name}.evidence.json`), result = path.join(out, `${name}.result.json`);
    await writeFile(file, JSON.stringify(evidence, null, 1) + '\n');
    await driver.runOracle({ folder, evidence: file, result, ms: 60000 });
    const checks = JSON.parse(await readFile(result, 'utf8')).checks;
    return checks.flatMap(check => Object.entries(check.measured).filter(([, rule]) => !rule.passed).map(([id]) => id)).sort();
  };
  const before = await failing(base, 'base');
  for (const [name, drift, step] of [['drift', 1e-3, 0], ['step', 0, 2000]]) {
    let t = 0;  // true session ms
    const mono = () => 7_000_000 + t;
    const wall = () => origin + t * (1 + drift) + (t >= 50_000 ? step : 0);
    // The driver reads pairs from before Record (record.requested) until it learns the origin,
    // at record observed, and maps the origin at its own instant (t = 0).
    const pairs = [];
    for (t = base.actions.record.requestedAtMs; t < base.actions.record.observedAtMs; t += 50) pairs.push(wallPair({ wall, mono }));
    t = base.actions.record.observedAtMs;
    const clock = sessionClock(origin, { pairs, wall, mono });
    const measured = via => ({ ...base,
      actions: Object.fromEntries(Object.entries(base.actions).map(([key, a]) => [key, { requestedAtMs: via(a.requestedAtMs), observedAtMs: via(a.observedAtMs) }])),
      resumeRequests: base.resumeRequests.map(r => ({ ...r, atMs: via(r.atMs) })) });
    const monotonicRun = measured(ms => { t = ms; return clock.at(mono()); });
    // Mapped at the origin's own instant, no drift remains: this wall clock is linear and unrounded,
    // so the mapping is exact up to floating point (#58 review: a 0.9 ms error must fail here).
    const bound = 0.01;
    for (const [key, a] of Object.entries(monotonicRun.actions)) {
      assert.ok(Math.abs(a.requestedAtMs - base.actions[key].requestedAtMs) <= bound, `${name}: ${key} requested ${a.requestedAtMs}`);
      assert.ok(Math.abs(a.observedAtMs - base.actions[key].observedAtMs) <= bound, `${name}: ${key} observed ${a.observedAtMs}`);
    }
    // now() follows the monotonic clock whatever the wall clock does after the mapping.
    t = 90_000;
    assert.ok(Math.abs(clock.now() - 90_000) <= bound, `${name}: now() ${clock.now()}`);
    const viaWall = await failing(measured(ms => { t = ms; return wall() - origin; }), `${name}-wall`);
    assert.deepEqual(await failing(monotonicRun, `${name}-monotonic`), before, `${name}: the monotonic session clock keeps every verdict`);
    assert.ok(viaWall.includes('O5.selection') && viaWall.some(id => !before.includes(id)),
      `${name}: a wall-clock session clock changes verdicts (me:true falls before the request, as on the hosted VM): ${JSON.stringify(viaWall)}`);
  }
});

// Mac #149: an app maps the origin when its recording starts, the driver about 2.35 s later, and
// a hosted VM's wall clock slewed 510 ppm in between. A mapping read when the driver learns the
// origin put the driver's meOff request 1.2 ms after the app's selection, which the app stamped
// at once (O5.selection). The synthetic run's meOff is moved to 0.2 ms before its selection: mapped
// at its own instant from the pairs read since before Record, the origin keeps every verdict; a
// mapping read 2.4 s late (the old sessionClock) fails O5.selection.
test('a slewing wall clock never moves the origin the driver learns late (Mac #149)', async () => {
  const folder = await synthetic(), out = path.dirname(folder);
  const base = JSON.parse(await readFile(path.join(out, 'evidence.json'), 'utf8')), origin = base.timelineOriginMs;
  const session = JSON.parse(await readFile(path.join(folder, 'session.json'), 'utf8'));
  const off = session.events.find(e => e.type === 'selection' && e.me === false);
  const tight = { ...base, actions: { ...base.actions, meOff: { ...base.actions.meOff, requestedAtMs: off.atMs - 0.2 } } };
  const verdicts = async (evidence, name) => {
    const file = path.join(out, `${name}.evidence.json`), result = path.join(out, `${name}.result.json`);
    await writeFile(file, JSON.stringify(evidence, null, 1) + '\n');
    await driver.runOracle({ folder, evidence: file, result, ms: 60000 });
    const checks = JSON.parse(await readFile(result, 'utf8')).checks;
    return checks.flatMap(check => Object.entries(check.measured).filter(([, rule]) => !rule.passed).map(([id]) => id)).sort();
  };
  const truth = await verdicts(tight, 'tight');
  assert.ok(!truth.includes('O5.selection'), `the true times pass O5.selection: ${JSON.stringify(truth)}`);
  let t = 0;  // true session ms
  const mono = () => 7_000_000 + t, wall = () => origin + t * (1 + 510e-6);
  const learned = 2400;
  const pairs = [];
  for (t = base.actions.record.requestedAtMs; t < learned; t += 50) pairs.push(wallPair({ wall, mono }));
  t = learned;
  const late = sessionClock(origin, { wall, mono }), atOrigin = sessionClock(origin, { pairs, wall, mono });
  const measured = clock => ({ ...tight, actions: Object.fromEntries(Object.entries(tight.actions).map(([key, a]) =>
    [key, { requestedAtMs: (t = a.requestedAtMs, clock.at(mono())), observedAtMs: (t = a.observedAtMs, clock.at(mono())) }])) });
  const fixed = measured(atOrigin);
  assert.ok(Math.abs(fixed.actions.meOff.requestedAtMs - tight.actions.meOff.requestedAtMs) < 0.05, `meOff ${fixed.actions.meOff.requestedAtMs}`);
  assert.deepEqual(await verdicts(fixed, 'at-origin'), truth, 'mapped at its own instant: every verdict kept');
  const old = await verdicts(measured(late), 'late');
  assert.ok(old.includes('O5.selection'), `a mapping read 2.4 s late fails O5.selection, as on Mac #149: ${JSON.stringify(old)}`);
});

// Bracketing: interpolated between the two readings around the origin. A wall-clock step is never
// interpolated across (Codex on #58: readings at -100 and +100 ms with a +2000 ms step at the
// origin put it 90.91 ms off): the origin is placed on the side of the step it was read on. It
// is never placed before the first pair or after the reading now, so a step back that leaves its
// wall time once in that window still maps; an origin no stretch places, or one a step near it
// lets both sides place, throws.
test('sessionClock places the origin between the readings that bracket it, never across a wall step', () => {
  let t = 0;
  const mono = () => 1000 + t, near = (actual, expected, message, within = 1e-6) => assert.ok(Math.abs(actual - expected) < within, `${message}: ${actual}`);
  const readings = (wall, times) => times.map(at => { t = at; return wallPair({ wall, mono }); });
  const slewing = () => 5_000_000 + t * (1 + 500e-6);
  let pairs = readings(slewing, [-100, -60, -20, 20, 60, 100]);
  t = 5000;
  near(sessionClock(5_000_000 + 30 * (1 + 500e-6), { pairs, wall: slewing, mono }).originMono, 1030, 'between -20 and 60: interpolated');
  assert.throws(() => sessionClock(5_000_000 - 300 * (1 + 500e-6), { pairs, wall: slewing, mono }), /fits no stretch/, 'before the first pair');
  near(sessionClock(5_000_000 + 5000 * (1 + 500e-6), { wall: slewing, mono }).originMono, 6000, 'no pairs: the reading now, as before');
  // A 2000 ppm slew (no step) across a 2 s gap between readings is a slew: interpolated.
  const fast = () => Math.floor(5_000_000 + t * 1.002);
  pairs = readings(fast, [0, 2000, 2100]);
  t = 2400;
  near(sessionClock(Math.floor(5_000_000 + 1000 * 1.002), { pairs, wall: fast, mono }).originMono, 2000, 'a fast slew across a 2 s gap', 1.5);
  // A +2000 ms step at t = 0, readings at -100 and +100: the origin read just before the step
  // (5_000_000) or just after it (5_002_000) is t = 0 either way; a wall time it skipped fits none.
  const forward = () => 5_000_000 + t + (t >= 0 ? 2000 : 0);
  pairs = readings(forward, [-100, 100]);
  t = 2400;
  near(sessionClock(5_000_000, { pairs, wall: forward, mono }).originMono, 1000, 'read before the step');
  near(sessionClock(5_002_000, { pairs, wall: forward, mono }).originMono, 1000, 'read after the step');
  assert.throws(() => sessionClock(5_001_000, { pairs, wall: forward, mono }), /fits no stretch/, 'a wall time the step skipped');
  // The mirror: a -2000 ms step at t = 0. The origin's wall time comes round again only after
  // the reading now, so it is t = 0.
  const back = () => 5_000_000 + t - (t >= 0 ? 2000 : 0);
  pairs = readings(back, [-100, 100]);
  t = 200;
  near(sessionClock(5_000_000, { pairs, wall: back, mono }).originMono, 1000, 'a step back after the origin');
  // Readings on to 2400 ms: wall times from just before the step come round again inside them.
  pairs = readings(back, Array.from({ length: 26 }, (_, i) => -100 + 100 * i));
  t = 2500;
  assert.throws(() => sessionClock(5_000_000 - 50, { pairs, wall: back, mono }), /stepped near the origin/, 'two instants read that wall time');
  near(sessionClock(5_000_000 + 300, { pairs, wall: back, mono }).originMono, 1000 + 2300, 'a wall time read only after the step back');
  // A +50 ms step at t = 0 with readings 100 ms apart, the origin read at t = -1: both sides of
  // the step could have read it, so it throws, naming a step (not a step back).
  const small = () => 5_000_000 + t + (t >= 0 ? 50 : 0);
  pairs = readings(small, [-500, -400, -300, -200, -100, 0, 100, 200, 300, 400, 500]);
  t = 2400;
  assert.throws(() => sessionClock(4_999_999, { pairs, wall: small, mono }), /stepped near the origin/, 'a step smaller than the reading gap');
  // Review of #58: a +100 ms step just after the origin, at the end of a 3 s gap, on a wall clock
  // slewing 1000 ppm. The side the origin was read on lands just past its window; it must not
  // lose to the other side silently, 100 ms off. It throws (the driver reads every 10 ms during
  // Record, so its gaps are never this long).
  pairs = [{ mono: 0, wall: 5_000_000 }, { mono: 3000, wall: 5_003_103 }, { mono: 3100, wall: 5_003_203 }];
  t = 2200;
  assert.throws(() => sessionClock(5_003_002, { pairs, wall: () => 5_003_303, mono: () => 3200 }), /stepped near the origin/, 'never the far side, silently');
  // A reader descheduled mid-reading (its midpoint 2 ms off, spread 5 ms) is no step: the origin
  // maps within that uncertainty instead of throwing.
  pairs = [{ mono: 900, wall: 4_999_900, spread: 0 }, { mono: 1002, wall: 5_000_000, spread: 5 }, { mono: 1100, wall: 5_000_100, spread: 0 }];
  t = 200;
  near(sessionClock(5_000_050, { pairs, wall: () => 5_000_000 + t, mono }).originMono, 1050, 'a slow reading, no step', 2);
});

// muteHold judges only A's spans that were open when the mute began (Mac #153: a span that had
// ended 12 s before the mute was reported as ended during it).
test('muteHold counts only spans open at the mute as ended during it', () => {
  const span = (id, ended) => ({ id, sourceTrackId: 'A', kind: 'remote', ...(ended ? { ended: true } : {}) });
  const atMute = { tracks: [span('a1', true), span('a2', false)] };
  const open = new Set(atMute.tracks.filter(t => t.ended !== true).map(t => t.id));
  assert.deepEqual([...open], ['a2']);
  assert.deepEqual(driver.endedDuringMute(atMute, 'A', open), [], 'a1 ended before the mute');
  assert.deepEqual(driver.endedDuringMute({ tracks: [span('a1', true), span('a2', true)] }, 'A', open), ['a2'], 'a2 ended during it');
});

// The driver and its folder clock never read the wall clock: every time they record or wait on
// is monotonic (only the extension's origin is wall-clock, mapped once by sessionClock).
test('the driver and the folder clock read no wall clock', async () => {
  for (const file of ['scenarios/live-folder.mjs', 'scenarios/folder-clock.mjs']) {
    const code = (await readFile(path.join(root, file), 'utf8')).split('\n').filter(line => !line.trim().startsWith('//')).join('\n');
    assert.doesNotMatch(code, /\bDate\.now\b|\bDate\(|timeOrigin/, `${file} reads the wall clock`);
  }
});

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
    // origin may be a function: the extension takes the origin at Record, after the driver's first pair.
    if (source.includes('cassiniSessions')) return [{ tabId: 7, captureId: 'capture-test', startedAt: typeof origin === 'function' ? origin() : origin }];
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
  let origin = null;
  const { app, calls } = fakeApp(run, {
    async record() { calls.record++; origin = Date.now(); },
    snapshot: async () => (calls.record ? { path: folder, captureId: 'capture-test' } : null),
    async selectMe() { throw new Error('injected failure after Record'); },
  });
  const logged = [];
  await assert.rejects(runLiveFolder({ meeting: page, remote: page, app, worker: worker(() => origin), out: run.out, log: line => logged.push(line) }),
    /injected failure after Record/);
  assert.deepEqual([calls.record, calls.stop, calls.cleanup], [1, 1, [{ failed: true }]], 'Record asked, so Stop asked; cleanup once');
  assert.equal(await exitOf(run.browser).then(r => r.code), 0, 'the host saw its stdin end and exited');
  await poll(() => ended(connection.relay), 'the relay is gone', BOUND, { pollMs: 5 });
  const teardown = JSON.parse(await readFile(path.join(run.out, 'teardown.json'), 'utf8'));
  assert.deepEqual(teardown, { failed: true, stop: 'done', cleanup: 'done', relaysStopped: [connection.relay], launchersKilled: [] });
  const partial = JSON.parse(await readFile(path.join(run.out, 'evidence.partial.json'), 'utf8'));
  assert.match(partial.notes.failure, /injected failure after Record/);
  assert.equal(partial.captureId, 'capture-test');
  // The real driver's record times are session times on its monotonic clock: small and ordered,
  // never a monotonic reading against the wall-clock origin, nor a wall reading.
  const record = partial.actions.record;
  assert.ok(record.requestedAtMs >= -BOUND && record.requestedAtMs <= record.observedAtMs && record.observedAtMs < BOUND,
    `record on the session timeline: ${JSON.stringify(record)}`);
  await holder.close();
});

// The real driver maps the origin at its own instant (Mac #149). Its clocks are injected and
// advanced by the test, never by real time (E1): the extension takes the origin at Record, and
// the app's snapshot is released only after the test moves the clocks on 10 s, on a wall clock
// slewing 510 ppm and read in whole ms. Mapped when it is learned, the Record request would sit
// 5 ms after the origin; mapped from the pairs the driver read since before Record, it sits at it.
test('the driver maps the origin at the instant it was taken, not when it learns it', async () => {
  const run = await controlRun('origin-instant');
  await poll(async () => { const c = await controlConnections(run.host); return c[0]?.relay && c[0].frames.length ? c : null; },
    'the relay starts and logs the hello', BOUND, { pollMs: 5 });
  const folder = await synthetic();
  let t = 0, origin = null, recorded, release;
  const mono = () => 50_000 + t, wall = () => Math.floor(1_700_000_000_000 + t * (1 + 510e-6));
  const asked = new Promise(resolve => { recorded = resolve; }), released = new Promise(resolve => { release = resolve; });
  const { app, calls } = fakeApp(run, {
    async record() { calls.record++; origin = wall(); recorded(); },
    async snapshot() { await released; return { path: folder, captureId: 'capture-test' }; },
    async selectMe() { throw new Error('injected failure after Record'); },
  });
  const late = {
    async evaluate(fn) {
      const source = String(fn);
      if (source.includes('cassiniSessions')) return [{ tabId: 7, captureId: 'capture-test', startedAt: origin }];
      return worker(0).evaluate(fn);
    },
  };
  const running = runLiveFolder({ meeting: page, remote: page, app, worker: late, out: run.out, log: () => {}, clocks: { mono, wall } });
  await asked;
  t = 10_000;
  release();
  await assert.rejects(running, /injected failure after Record/);
  const partial = JSON.parse(await readFile(path.join(run.out, 'evidence.partial.json'), 'utf8'));
  const record = partial.actions.record;
  assert.ok(Math.abs(record.requestedAtMs) < 0.5, `record requested at the origin's instant: ${JSON.stringify(record)}`);
  assert.equal(record.observedAtMs, 10_000, `record observed when the test moved the clocks on: ${JSON.stringify(record)}`);
  assert.ok(partial.notes.originPairs >= 3, `pairs read across Record: ${partial.notes.originPairs}`);
});

// The driver reads the wall clock all through Record (ORIGIN_SAMPLE_MS), so a step during a long
// Record call is told from the origin around it. Here the injected clocks move only when the
// driver reads the wall clock while Record runs: 10 ms a reading. The extension takes the origin
// at t = 1000, the wall clock steps +300 ms at t = 1500 and Record returns at t = 2000. With only
// the readings before and after Record, both sides of the step would place the origin and the
// run would throw; read throughout, the Record request sits 1000 ms before the origin.
test('the driver reads the wall clock all through Record, so a step during it is no ambiguity', async () => {
  const run = await controlRun('origin-step');
  await poll(async () => { const c = await controlConnections(run.host); return c[0]?.relay && c[0].frames.length ? c : null; },
    'the relay starts and logs the hello', BOUND, { pollMs: 5 });
  const folder = await synthetic();
  let t = 0, origin = null, recording = false, done;
  const reached = new Promise(resolve => { done = resolve; });
  const reading = () => Math.floor(1_700_000_000_000 + t * (1 + 510e-6)) + (t >= 1500 ? 300 : 0);
  const mono = () => 50_000 + t;
  const wall = () => {
    if (recording) {
      t += 10;
      if (t === 1000) origin = reading();
      if (t >= 2000) done();
    }
    return reading();
  };
  const { app, calls } = fakeApp(run, {
    async record() {
      calls.record++; recording = true;
      // Bounded only so a driver that never reads the clock during Record fails instead of hanging.
      await poll(() => (t >= 2000 ? true : null), 'the driver reads the wall clock while Record runs', BOUND, { pollMs: 5 });
      await reached; recording = false;
    },
    snapshot: async () => ({ path: folder, captureId: 'capture-test' }),
    async selectMe() { throw new Error('injected failure after Record'); },
  });
  await assert.rejects(runLiveFolder({ meeting: page, remote: page, app, worker: worker(() => origin), out: run.out, log: () => {}, clocks: { mono, wall } }),
    /injected failure after Record/);
  const partial = JSON.parse(await readFile(path.join(run.out, 'evidence.partial.json'), 'utf8'));
  // Within the readings' own uncertainty: the clocks move inside each reading, so its monotonic
  // midpoint is 5 ms from the instant it read (its spread). Without readings through Record the
  // run throws instead (both sides of the step place the origin).
  assert.ok(Math.abs(partial.actions.record.requestedAtMs + 1000) <= 5, `record requested 1000 ms before the origin: ${JSON.stringify(partial.actions.record)}`);
  assert.ok(partial.notes.originPairs >= 200, `pairs read through Record: ${partial.notes.originPairs}`);
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

test('stopLaunchers kills a launcher still waiting on its FIFO, which is never the live host', async () => {
  const run = await controlRun('waiting-launcher', { node: '/bin/false' });
  await poll(async () => (await controlConnections(run.host))[0]?.pid, 'the launcher records the connection', BOUND, { pollMs: 5 });
  // /bin/sh <launcher> never runs the host's command: not live, and killed by its argv.
  assert.equal((await controlConnections(run.host))[0].live, false);
  assert.deepEqual(await stopLaunchers(run.host), [run.browser.pid]);
  assert.deepEqual(await exitOf(run.browser), { code: null, signal: 'SIGKILL' });
});

// A process that waits until it is killed, with exactly this argv (argv[0] as given).
const WAIT = 'setTimeout(() => {}, 600000)';
async function waiting(file, args) {
  const child = spawn(file, args, { stdio: 'ignore' });
  children.add(child);
  await poll(async () => runsCommand(await processArgv(child.pid), [file, ...args]), `${path.basename(file)} runs`, BOUND, { pollMs: 5 });
  return child;
}

test('a recycled pid is never the host: the whole command, argument by argument, with spaces in paths', async () => {
  // The host's interpreter is reached through a path with spaces, as a Mac app bundle's is.
  const folder = path.join(scratch, 'Installed Cassini.app', 'Contents');
  execFileSync('mkdir', ['-p', folder]);
  const node = path.join(folder, 'node');
  await symlink(process.execPath, node);
  const host = { pidDirectory: path.join(scratch, 'recycled-pids'), name: 'io.example.audio', command: [node, '-e', WAIT] };
  execFileSync('mkdir', ['-p', host.pidDirectory]);
  const record = async child => writeFile(path.join(host.pidDirectory, `${host.name}-${child.pid}.pid`), `${child.pid}\n`);
  // The host, with the browser's arguments after its command.
  const real = await waiting(node, ['-e', WAIT, 'chrome-extension://abc/']);
  // Pids the launcher once recorded, now other processes: the same interpreter with another
  // script, the host's path only as an argument, and the command's words joined into one argument.
  const decoys = [await waiting(node, ['-e', WAIT + ' ']), await waiting(process.execPath, ['-e', WAIT, node]),
    await waiting(process.execPath, ['-e', WAIT, [node, '-e', WAIT].join(' ')])];
  for (const child of [real, ...decoys]) await record(child);
  assert.deepEqual(await livePids(host), [real.pid]);
  for (const child of [real, ...decoys]) child.kill('SIGKILL');
});

test('stopRelays and stopLaunchers signal only the exact relay script and launcher, never a similarly named process', async () => {
  const observe = path.join(scratch, 'similar-names');
  const launcher = path.join(scratch, 'similar', 'io.example.control.launcher');
  const host = { observe, command: ['/nonexistent/host'], launcher };
  const relay = await waiting(process.execPath, ['-e', WAIT, RELAY]);
  const relayLike = await waiting(process.execPath, ['-e', WAIT, RELAY + '.backup']);
  const launcherLike = await waiting(process.execPath, ['-e', WAIT, launcher + '.old']);
  const launcherReal = await waiting(process.execPath, ['-e', WAIT, launcher]);
  // Four connections as their logs read: two relays still running, two launchers.
  const connection = async (n, { relayPid = null, hostPid = null }) => {
    const directory = path.join(observe, String(n));
    execFileSync('mkdir', ['-p', directory]);
    if (hostPid !== null) await writeFile(path.join(directory, 'host'), `${hostPid} ${process.pid}\n`);
    await writeFile(path.join(directory, 'frames.jsonl'), relayPid === null ? '' : JSON.stringify({ relay: relayPid, startedWall: 1 }) + '\n');
  };
  await connection(1, { relayPid: relay.pid });
  await connection(2, { relayPid: relayLike.pid });
  await connection(3, { hostPid: launcherLike.pid });
  await connection(4, { hostPid: launcherReal.pid });
  assert.deepEqual(await stopRelays(host), [relay.pid]);
  assert.deepEqual(await exitOf(relay), { code: null, signal: 'SIGTERM' });
  assert.deepEqual(await stopLaunchers(host), [launcherReal.pid]);
  assert.deepEqual(await exitOf(launcherReal), { code: null, signal: 'SIGKILL' });
  assert.deepEqual([relayLike.exitCode, relayLike.signalCode, launcherLike.exitCode, launcherLike.signalCode], [null, null, null, null],
    'the similarly named processes survive');
  relayLike.kill('SIGKILL'); launcherLike.kill('SIGKILL');
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
  // The kill may come before the stand-in ran its first command (macOS checks a new
  // executable on its first exec): then no pid was recorded and nothing else ran.
  const recorded = await readFile(path.join(directory, 'pid'), 'utf8').catch(error => { if (error.code === 'ENOENT') return null; throw error; });
  if (recorded !== null) assert.ok(await ended(Number(recorded.trim())), 'the hung check.py was killed');
});
