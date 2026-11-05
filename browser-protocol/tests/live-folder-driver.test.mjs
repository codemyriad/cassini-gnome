// Unit tests for the live-folder driver's pieces that need no browser (shared test 2, D68):
// the folder clock against make.py's synthetic folder, its tolerant reads and bounded
// waits, the pid-recording native host launcher, and the scenario constants the driver uses.
import assert from 'node:assert/strict';
import { execFileSync, spawn } from 'node:child_process';
import { mkdtemp, readdir, readFile, rm, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { eventsOf, extent, F, folderClock, S } from '../scenarios/folder-clock.mjs';
import { hostPids, livePids, writeNativeHost } from '../scenarios/native-launcher.mjs';
import { loadScenario } from '../scenarios/live-folder.mjs';

const root = path.resolve(import.meta.dirname, '..');
const scratch = await mkdtemp(path.join(os.tmpdir(), 'live-folder-driver-'));
test.after(() => rm(scratch, { recursive: true, force: true }));

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
