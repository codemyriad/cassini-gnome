import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { readFile, mkdir, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { connectReceiveSlots, switchReceiveSlot, installZoomLikeAudio } from '../scenarios/product-topologies.mjs';
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || 'playwright');
const server = createServer(async (request, response) => {
  if (request.url === '/') { response.end('<!doctype html><title>Controlled product topology fixture</title><body>'); return; }
  if (!['/capture.js', '/pcm-worklet.js'].includes(request.url)) { response.writeHead(404).end(); return; }
  response.setHeader('Content-Type', 'text/javascript');
  response.end(await readFile(path.join(root, 'extension', request.url.slice(1))));
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const address = `http://127.0.0.1:${server.address().port}/`;
let browser, currentPackets = [];
const results = [];
function pcm(packets, id) { return Buffer.concat(packets.filter(p => p.type === 'audio' && p.id === id).map(p => Buffer.from(p.pcm, 'base64'))); }
function power(data, frequency, rate = 48000) {
  const count = 12000, start = data.length / 2 - count;
  assert.ok(start >= 0, 'enough recorded frames for spectral analysis');
  let real = 0, imaginary = 0;
  for (let i = 0; i < count; i++) {
    const sample = data.readInt16LE((start + i) * 2) / 32768;
    real += sample * Math.cos(2 * Math.PI * frequency * i / rate);
    imaginary += sample * Math.sin(2 * Math.PI * frequency * i / rate);
  }
  return (real ** 2 + imaginary ** 2) / count ** 2;
}
async function waitFor(check) {
  const deadline = Date.now() + 15000;
  while (!check()) { assert.ok(Date.now() < deadline, 'timed out waiting for recorded samples'); await new Promise(resolve => setTimeout(resolve, 25)); }
}
async function start(page, packets, errors) {
  await page.exposeFunction('recordPacket', packet => packets.push(packet));
  await page.exposeFunction('recordFailure', message => errors.push(message));
  await page.evaluate(() => {
    window.fixtureStopped = false; window.fixtureReady = false;
    window.addEventListener('message', async event => {
      if (event.source !== window || event.data?.channel !== 'cassini-page') return;
      const message = event.data;
      if (message.type === 'packet') {
        await window.recordPacket(message.packet);
        window.postMessage({ channel: 'cassini-control', type: 'ack', token: 'fixture', request: message.request }, '*');
      } else if (message.type === 'stopped') window.fixtureStopped = true;
      else if (message.type === 'ready') window.fixtureReady = true;
      else if (['error', 'warning'].includes(message.type)) window.recordFailure(message.type);
    });
    window.postMessage({ channel: 'cassini-control', type: 'start', token: 'fixture', origin: Date.now(),
      moduleURL: location.origin + '/capture.js', workletURL: location.origin + '/pcm-worklet.js' }, '*');
  });
  await page.waitForFunction(() => fixtureReady);
}
try {
  browser = await chromium.launch({ headless: true, args: ['--autoplay-policy=no-user-gesture-required', '--allow-loopback-in-peer-connection', '--disable-features=WebRtcHideLocalIpsWithMdns'] });
  console.log('Chromium', browser.version());
  // Connected pre-join topology: receive slots exist, local preview sends RTP,
  // but no inbound audio report exists until the synthetic Join action below.
  {
    const context = await browser.newContext(), page = await context.newPage(), remote = await context.newPage();
    await page.addInitScript({ path: path.join(root, 'extension/page-hook.js') });
    await page.addInitScript(() => {
      window.discoveryEvidence = [];
      addEventListener('message', event => {
        if (event.source === window && event.data?.channel === 'cassini-page' && event.data.type === 'discovery')
          discoveryEvidence.push(event.data);
      });
    });
    await page.goto(address); await remote.goto(address);
    await connectReceiveSlots(page, remote, [[440], [660], [880]], { prejoin: true });
    await page.evaluate(() => {
      window.prejoinSamples = [];
      window.prejoinClock = setInterval(async () => {
        const stats = await topologyPeer.getStats();
        const inbound = [...stats.values()].filter(report => report.type === 'inbound-rtp' && report.kind === 'audio');
        prejoinSamples.push(inbound.map(report => report.packetsReceived));
        window.postMessage({channel:'cassini-control',type:'discover'}, '*');
      }, 1000);
    });
    await page.waitForFunction(() => prejoinSamples.length >= 3 && discoveryEvidence.length >= 3);
    const before = await page.evaluate(() => ({ samples: prejoinSamples, evidence: discoveryEvidence.at(-1) }));
    assert.ok(before.samples.every(sample => sample.length === 0), 'pre-join receives no inbound audio RTP');
    assert.equal(before.evidence.sourceCount, 3, 'only three receivers; preview sender excluded');
    assert.equal(before.evidence.heard, false);
    await switchReceiveSlot(remote, 0, [440]);
    await page.waitForFunction(() => discoveryEvidence.at(-1)?.heard === true);
    const joined = await page.evaluate(() => discoveryEvidence.at(-1));
    assert.equal(joined.sourceCount, 3);
    results.push({ model: 'Pre-join receive slots then inbound RTP', status: 'passed',
      chromium: browser.version(), prejoin: before, joined, liveProductTest: false });
    console.log('PASS pre-join has no heard evidence; inbound audio sets the latch');
    await context.close();
  }
  for (const [name, frequencies] of [['Teams mixed receiver', [[440, 880]]], ['Meet three switching slots', [[440], [660], [880]]], ['Zoom PC-free path', null]]) {
    const context = await browser.newContext(), page = await context.newPage(), remote = await context.newPage();
    const packets = [], errors = []; currentPackets = packets;
    await page.addInitScript({ path: path.join(root, 'extension/page-hook.js') });
    await page.goto(address); await remote.goto(address);
    if (frequencies) await connectReceiveSlots(page, remote, frequencies);
    else await installZoomLikeAudio(page);
    await start(page, packets, errors);
    if (frequencies) {
      await waitFor(() => packets.filter(p => p.type === 'track').length === frequencies.length && packets.filter(p => p.type === 'track').every(track => pcm(packets, track.id).length >= 96000));
      const tracks = packets.filter(p => p.type === 'track');
      assert.ok(tracks.every(track => track.kind === 'remote'));
      tracks.forEach((track, index) => {
        const audio = pcm(packets, track.id);
        for (const tone of frequencies[index]) assert.ok(power(audio, tone) > 0.001, `${name}: ${tone} Hz present`);
        for (const tone of [440, 660, 880].filter(tone => !frequencies[index].includes(tone))) {
          assert.ok(power(audio, tone) < power(audio, frequencies[index][0]) * 0.01, `${name}: source isolation`);
        }
      });
      if (frequencies.length === 3) {
        const before = pcm(packets, tracks[0].id).length;
        const receiver = await page.evaluate(() => topologyPeer.getReceivers()[0].track.id);
        await switchReceiveSlot(remote, 0, [990]);
        await waitFor(() => pcm(packets, tracks[0].id).length >= before + 96000);
        assert.equal(await page.evaluate(() => topologyPeer.getReceivers()[0].track.id), receiver);
        assert.equal(packets.filter(p => p.type === 'track').length, 3, 'speaker switch preserves receiver-source identity');
        const audio = pcm(packets, tracks[0].id);
        assert.ok(power(audio, 990) > 0.001 && power(audio, 440) < power(audio, 990) * 0.01);
      }
    } else {
      await page.waitForFunction(() => zoomLikeAudio.audio.currentTime > 1);
      assert.equal(packets.length, 0, 'PC-free Web Audio exposes no invented WebRTC source; tab fallback is required');
    }
    await page.evaluate(() => window.postMessage({ channel: 'cassini-control', type: 'stop', token: 'fixture' }, '*'));
    await page.waitForFunction(() => fixtureStopped);
    assert.deepEqual(errors, []);
    results.push({ model: name, status: 'passed', chromium: browser.version(), liveProductTest: false });
    console.log('PASS', name); await context.close();
  }
  await mkdir(path.join(root, 'artifacts'), { recursive: true });
  await writeFile(path.join(root, 'artifacts/topology-results.json'), JSON.stringify(results, null, 2));
} catch (error) {
  await mkdir(path.join(root, 'artifacts'), { recursive: true });
  await writeFile(path.join(root, 'artifacts/topology-failure.json'), JSON.stringify({ error: error.stack, results, packets: currentPackets }, null, 2));
  throw error;
} finally { await browser?.close(); server.close(); }
