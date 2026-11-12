import assert from 'node:assert/strict';
import { watch } from 'node:fs';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import { eventValue } from '../tests/support/event-value.mjs';

export async function stopRecordingWorker(context, meetingPage, extensionId) {
  const session = await context.newCDPSession(meetingPage);
  const versionEvent = 'ServiceWorker.workerVersionUpdated';
  let versions = [];
  const remember = event => { versions = event.versions; };
  // Enable can publish the running version before its command reply.
  session.on(versionEvent, remember);
  try {
    await session.send('ServiceWorker.enable');
    const worker = await eventValue(changed => {
      session.on(versionEvent, changed);
      return () => session.off(versionEvent, changed);
    }, () => versions.find(item => item.scriptURL === `chrome-extension://${extensionId}/background.js` &&
      item.runningStatus === 'running'), { label: 'recording worker in CDP', timeout: 5000 });
    await session.send('ServiceWorker.stopWorker', { versionId: worker.versionId });
  } finally {
    session.off(versionEvent, remember);
    await session.detach();
  }
}

function sessionValue(directory, read, label, timeout) {
  return eventValue((changed, failed) => {
    const watcher = watch(directory, changed);
    watcher.on('error', failed);
    return () => watcher.close();
  }, read, { label, timeout });
}

// Shared by both app adapters; the meeting page must already have an audible generated source.
// With library, the host's meeting must stay inside that private test Library.
export async function workerDeathDuringRecording({ context, meetingPage, popup, extensionId, mode = 'tracks', library }) {
  const status = () => popup.evaluate(() => chrome.runtime.sendMessage({ target: 'background', type: 'status' }));
  // The companion popup has no product recording controls. Keep the fixture
  // on the trusted popup RPC, which is also used by the reload regression.
  const started = await popup.evaluate(mode => chrome.runtime.sendMessage({target: 'background', type: 'start', mode}), mode);
  assert.ok(!started.error, `worker fixture could not start: ${JSON.stringify(started)}`);
  assert.ok(started.recording && started.path, `Start did not return its recording path: ${JSON.stringify(started)}`);
  if (library) assert.ok(path.resolve(started.path).startsWith(path.resolve(library) + path.sep), `host wrote outside its test Library: ${started.path}`);
  await sessionValue(started.path, async () => {
    const manifest = JSON.parse(await readFile(path.join(started.path, 'session.json')));
    const track = manifest.tracks[0];
    if (track) {
      const wav = await readFile(path.join(started.path, track.file));
      return (wav.length - 44) / (track.channels * 2) >= track.sampleRate * 5;
    }
    return false;
  }, 'five seconds of saved audio before worker stop', 20000);
  const before = await status();
  assert.equal(before.path, started.path);
  assert.ok(before.recording && before.tracks, `recording ended before worker stop: ${JSON.stringify(before)}`);
  await stopRecordingWorker(context, meetingPage, extensionId);
  await popup.reload();
  const [after, metadata] = await Promise.all([
    status(),
    sessionValue(before.path, async () => {
      const manifest = JSON.parse(await readFile(path.join(before.path, 'session.json')));
      return manifest.state === 'interrupted' && manifest;
    }, 'durable worker interruption checkpoint', 15000),
  ]);
  assert.equal(after.recording, false);
  assert.match(after.message, /interrupted/i);
  assert.equal(after.path, before.path);
  if (before.callGeneration) {
    const restored = await popup.evaluate(async tabId => {
      const value = await chrome.storage.session.get('cassiniCallGenerations');
      return value.cassiniCallGenerations?.find(row => row.tabId === tabId)?.generation;
    }, before.tabId);
    assert.equal(restored, before.callGeneration, 'worker restart changed the retained call identity');
  }
  assert.equal(await popup.evaluate(() => chrome.action.getBadgeText({})), '');
  assert.ok(metadata.tracks.some(track => track.frames > track.sampleRate * 4), 'audio accepted before worker death survived');
  const retained = await Promise.all(metadata.tracks.map(track => readFile(path.join(before.path, track.file))));
  assert.ok(retained.some(wav => wav.subarray(44).some(byte => byte !== 0)), 'the generated tone survived worker death');
  if (mode === 'tab') assert.equal(await popup.evaluate(async () => (await chrome.runtime.getContexts({ contextTypes: ['OFFSCREEN_DOCUMENT'] })).length), 0);
  return { path: before.path, status: after, manifest: metadata };
}
