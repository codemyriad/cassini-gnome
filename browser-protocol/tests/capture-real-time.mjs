// D68.1 regression check: a capture longer than 30 s keeps real time, and the constant that keeps
// it there never reaches the captured audio.
//
// Chromium moves an AudioContext whose output has been all zeros for about 30 s (its silent sink
// suspender) off the audio device onto a timer, which can deliver far less than real time. The
// extension's capture graph outputs silence by design, and the worklet stamps every frame by its
// context's render clock, so the extension keeps each capture context's output non-zero with an
// inaudible constant (keepRealTime in extension/capture.js). This drives the production capture()
// (extension/capture.js and pcm-worklet.js, served as the page loads them) on two captures at once,
// each in its own context:
// - a tone input, which must keep real time after the suspender would have engaged;
// - a silent input, whose every captured sample, and every sample of the WAV an app saves from it,
//   must be exactly zero, and which must be a silent track by the D56/D57 rule (GNOME R76).
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || 'playwright');

const RATE = 48000;
const AUDIO_SECONDS = 45;   // well past the suspender's 30 s
const AFTER_SECONDS = 31;   // the rate is measured from here on, after the suspender would engage
const BOUND_SECONDS = 90;   // one deadline from the captures' start, before any audio too
const MIN_RATE = 0.97;      // audio seconds per wall second after AFTER_SECONDS of audio

// D56/D57 (GNOME R76): a track is silent with fewer than 32 non-zero frames. Frames are
// floor(rate x 32 / 1000) samples, non-overlapping from the first sample; a trailing partial frame
// never counts; a frame is non-zero if any sample is not 0.
function nonZeroFrames(samples, rate) {
  const frame = Math.floor(rate * 32 / 1000);
  let count = 0;
  for (let start = 0; start + frame <= samples.length; start += frame) {
    for (let i = start; i < start + frame; i++) if (samples[i] !== 0) { count++; break; }
  }
  return count;
}
// The 16-bit mono WAV an app saves from the captured PCM, and its samples read back.
function wav(pcm) {
  const header = Buffer.alloc(44);
  header.write('RIFF', 0); header.writeUInt32LE(36 + pcm.length, 4); header.write('WAVEfmt ', 8);
  header.writeUInt32LE(16, 16); header.writeUInt16LE(1, 20); header.writeUInt16LE(1, 22);
  header.writeUInt32LE(RATE, 24); header.writeUInt32LE(RATE * 2, 28); header.writeUInt16LE(2, 32); header.writeUInt16LE(16, 34);
  header.write('data', 36); header.writeUInt32LE(pcm.length, 40);
  return Buffer.concat([header, pcm]);
}
const samplesOf = data => Array.from({ length: (data.length - 44) / 2 }, (_, i) => data.readInt16LE(44 + i * 2));

const server = createServer(async (request, response) => {
  if (request.url === '/') { response.end('<!doctype html><title>Capture keeps real time</title><body>'); return; }
  if (!['/capture.js', '/pcm-worklet.js'].includes(request.url)) { response.writeHead(404).end(); return; }
  response.setHeader('Content-Type', 'text/javascript');
  response.end(await readFile(path.join(root, 'extension', request.url.slice(1))));
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const address = `http://127.0.0.1:${server.address().port}/`;
const scratch = await mkdtemp(path.join(os.tmpdir(), 'capture-real-time-'));
let browser, deadline;
try {
  browser = await chromium.launch({ headless: true, args: ['--autoplay-policy=no-user-gesture-required'] });
  const page = await browser.newPage();
  await page.goto(address);
  // Per capture: [wall ms, cumulative frames] at each audio packet, and (silent) its PCM, in Node.
  const arrivals = { tone: [], silent: [] }, frames = { tone: 0, silent: 0 }, silentPCM = [], retries = [];
  // Completion: resolved when both captures hold AUDIO_SECONDS, rejected by a failure or the one deadline.
  let finish, fail;
  const done = new Promise((resolve, reject) => { finish = resolve; fail = reject; });
  done.catch(() => {});  // awaited below; a failure during setup is not an unhandled rejection
  const started = Date.now();
  deadline = setTimeout(() => fail(new Error(`after ${BOUND_SECONDS} s: tone ${(frames.tone / RATE).toFixed(1)} s, ` +
    `silent ${(frames.silent / RATE).toFixed(1)} s of audio` + (retries.length ? `; capture retries: ${JSON.stringify(retries)}` : ''))), BOUND_SECONDS * 1000);
  await page.exposeFunction('recordAudio', (id, pcm) => {
    const bytes = Buffer.from(pcm, 'base64');
    frames[id] += bytes.length / 2; arrivals[id].push([Date.now(), frames[id]]);
    if (id === 'silent') silentPCM.push(bytes);
    if (frames.tone / RATE >= AUDIO_SECONDS && frames.silent / RATE >= AUDIO_SECONDS) finish();
  });
  await page.exposeFunction('recordFailure', (id, message) => fail(new Error(`${id} capture failed: ${message}`)));
  await page.exposeFunction('recordRetry', (id, message) => { retries.push(`${id}: ${message}`); });
  await page.evaluate(async () => {
    const { capture } = await import('/capture.js');
    // Each input from its own context; their pace does not matter, only the capture contexts'.
    const input = tone => {
      const context = new AudioContext({ sampleRate: 48000 }), output = context.createMediaStreamDestination();
      if (tone) { const oscillator = context.createOscillator(); oscillator.connect(output); oscillator.start(); }
      return context.resume().then(() => output.stream);
    };
    window.capturing = [];
    for (const [id, tone] of [['tone', true], ['silent', false]]) {
      const send = async packet => { if (packet.type === 'audio') await window.recordAudio(id, packet.pcm); };
      window.capturing.push(await capture({ stream: await input(tone), id, label: id, kind: 'local', origin: Date.now(),
        workletURL: location.origin + '/pcm-worklet.js', send, fail: message => window.recordFailure(id, String(message)),
        warn: message => window.recordRetry(id, String(message)) }));
    }
  });
  await done;
  clearTimeout(deadline);
  // The rate after the suspender would engage: between the first arrival past AFTER_SECONDS of audio
  // and the last, both counted at arrival, so no packet's frames are credited before its time.
  const tone = arrivals.tone, from = tone.find(([, total]) => total / RATE >= AFTER_SECONDS), to = tone.at(-1);
  const audio = (to[1] - from[1]) / RATE, wall = (to[0] - from[0]) / 1000, rate = audio / wall;
  const overall = (to[1] - tone[0][1]) / RATE / ((to[0] - tone[0][0]) / 1000);
  console.log(`capture-real-time: after ${AFTER_SECONDS} s, ${audio.toFixed(2)} s of audio in ${wall.toFixed(2)} s of wall time ` +
    `(rate ${rate.toFixed(3)}; overall ${overall.toFixed(3)}; retries ${retries.length}; ${((Date.now() - started) / 1000).toFixed(1)} s in all)`);
  assert.ok(rate >= MIN_RATE, `after ${AFTER_SECONDS} s the capture clock ran at ${rate.toFixed(3)} of real time (minimum ${MIN_RATE})`);
  // The constant never leaks: the silent capture is exactly zero, captured and saved.
  const pcm = Buffer.concat(silentPCM), captured = Array.from({ length: pcm.length / 2 }, (_, i) => pcm.readInt16LE(i * 2));
  assert.ok(captured.length >= AUDIO_SECONDS * RATE, `silent capture: ${captured.length} samples`);
  assert.equal(captured.findIndex(sample => sample !== 0), -1, 'every captured sample of the silent input is exactly zero');
  const file = path.join(scratch, 'silent.wav');
  await writeFile(file, wav(pcm));
  const saved = samplesOf(await readFile(file));
  assert.equal(saved.length, captured.length);
  assert.equal(saved.findIndex(sample => sample !== 0), -1, 'every saved WAV sample of the silent input is exactly zero');
  const nonZero = nonZeroFrames(saved, RATE);
  assert.ok(nonZero < 32, `the silent input is a silent track (D56/D57): ${nonZero} non-zero 32 ms frames`);
  console.log(`capture-real-time: silent input ${captured.length} samples, all zero; ${nonZero} non-zero 32 ms frames (silent track)`);
  await page.evaluate(() => Promise.all(window.capturing.map(recorder => recorder.stop())));
} finally {
  clearTimeout(deadline);
  await browser?.close();
  server.close();
  await rm(scratch, { recursive: true, force: true });
}
