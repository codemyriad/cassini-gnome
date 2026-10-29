// Loaded MV3 worker lifecycle. Default fixture host is portable; either app may
// set CASSINI_NATIVE_HOST to run this same adapter against its installed host.
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { mkdtemp, mkdir, writeFile, chmod, rm } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { workerDeathDuringRecording } from '../scenarios/worker-recovery.mjs';
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const extension = path.resolve(process.env.CASSINI_TEST_EXTENSION || path.join(root, 'extension'));
const temporary = await mkdtemp(path.join(os.tmpdir(), 'cassini-worker-'));
const profile = path.join(temporary, 'profile');
const server = createServer((_request, response) => response.end('<!doctype html><title>Cassini worker fixture</title><button>Join</button>'));
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
let context;
try {
  const launch = extra => chromium.launchPersistentContext(profile, {
    channel: 'chromium', headless: true, ignoreDefaultArgs: ['--mute-audio'],
    args: [`--disable-extensions-except=${extension}`, `--load-extension=${extension}`, '--autoplay-policy=no-user-gesture-required', ...extra]
  });
  context = await launch([]);
  const initial = context.serviceWorkers()[0] || await context.waitForEvent('serviceworker');
  const id = new URL(initial.url()).host;
  await context.close();
  context = await launch([`--allowlisted-extension-id=${id}`]);
  const worker = context.serviceWorkers()[0] || await context.waitForEvent('serviceworker');
  let host = process.env.CASSINI_NATIVE_HOST;
  if (!host) {
    // No shell: the generated launcher imports only this checked-in test fixture.
    host = path.join(temporary, 'host.py');
    const fixture = path.join(root, 'tests/browser_host_fixture.py');
    await writeFile(host, `#!/usr/bin/env python3\nimport sys\nsys.argv=[${JSON.stringify(fixture)},${JSON.stringify(temporary)}]\nexec(compile(open(sys.argv[0]).read(),sys.argv[0],'exec'))\n`);
    await chmod(host, 0o700);
  }
  const hosts = path.join(profile, 'NativeMessagingHosts');
  await mkdir(hosts, { recursive: true });
  await writeFile(path.join(hosts, 'io.codemyriad.cassini.json'), JSON.stringify({ name: 'io.codemyriad.cassini', description: 'Cassini worker test', path: path.resolve(host), type: 'stdio', allowed_origins: [`chrome-extension://${id}/`] }));
  const meetingPage = await context.newPage();
  const errors = [];
  meetingPage.on('pageerror', error => errors.push(error.message));
  await meetingPage.goto(`http://127.0.0.1:${server.address().port}/`);
  await meetingPage.click('button');
  await meetingPage.evaluate(async () => {
    const audio = new AudioContext(), oscillator = audio.createOscillator(), output = audio.createMediaStreamDestination();
    oscillator.frequency.value = 880; oscillator.connect(output); oscillator.connect(audio.destination); oscillator.start(); await audio.resume();
    const element = document.createElement('audio'); element.muted = true; element.srcObject = output.stream; document.body.append(element); await element.play();
    window.workerFixture = { audio, oscillator, output, element };
  });
  const meetingTab = await worker.evaluate(async url => {
    const [selected] = await chrome.tabs.query({ active: true, currentWindow: true });
    const tab = { ...selected, url, title: 'Cassini worker fixture' }; chrome.tabs.query = async () => [tab]; return tab;
  }, meetingPage.url());
  const popup = await context.newPage();
  await popup.goto(`chrome-extension://${id}/popup.html`);
  for (const mode of (process.env.CASSINI_WORKER_TEST_MODES || 'tracks,tab').split(',')) {
    const current = context.serviceWorkers().find(item => item.url().includes(id)) || await context.waitForEvent('serviceworker');
    await current.evaluate(tab => { chrome.tabs.query = async () => [tab]; }, meetingTab);
    const result = await workerDeathDuringRecording({ context, meetingPage, popup, extensionId: id, mode });
    await rm(result.path, { recursive: true, force: true });
    console.log(`PASS worker death after Start (${mode}): saved audio, interruption/path, badge cleared${mode === 'tab' ? ', offscreen closed' : ''}`);
  }
  assert.deepEqual(errors, []);
} finally {
  await context?.close(); server.close(); await rm(temporary, { recursive: true, force: true });
}
