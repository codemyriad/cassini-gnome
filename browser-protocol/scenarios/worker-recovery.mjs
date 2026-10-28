import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import path from 'node:path';

export async function stopRecordingWorker(context, meetingPage, extensionId) {
  const session = await context.newCDPSession(meetingPage);
  let versions = [];
  session.on('ServiceWorker.workerVersionUpdated', event => { versions = event.versions; });
  await session.send('ServiceWorker.enable');
  const deadline = Date.now() + 5000;
  let worker;
  while (!(worker = versions.find(item => item.scriptURL === `chrome-extension://${extensionId}/background.js` && item.runningStatus === 'running'))) {
    assert.ok(Date.now() < deadline, 'recording worker did not appear in CDP');
    await new Promise(resolve => setTimeout(resolve, 25));
  }
  await session.send('ServiceWorker.stopWorker', { versionId: worker.versionId });
  await session.detach();
}

// Shared by both app adapters; the meeting page must already have an audible generated source.
export async function workerDeathDuringRecording({ context, meetingPage, popup, extensionId, mode = 'tracks' }) {
  const status = () => popup.evaluate(() => chrome.runtime.sendMessage({ target: 'background', type: 'status' }));
  await popup.click(mode === 'tracks' ? '#tracks' : '#tab');
  const deadline = Date.now() + 20000;
  let before, metadata;
  while (true) {
    before = await status();
    if (before.path && before.recording && before.tracks) {
      metadata = JSON.parse(await readFile(path.join(before.path, 'session.json')));
      const track = metadata.tracks[0];
      if (track) {
        const wav = await readFile(path.join(before.path, track.file));
        if ((wav.length - 44) / (track.channels * 2) >= track.sampleRate * 5) break;
      }
    }
    assert.ok(Date.now() < deadline, `no five-second recording before worker stop: ${JSON.stringify(before)}`);
    await new Promise(resolve => setTimeout(resolve, 50));
  }
  await stopRecordingWorker(context, meetingPage, extensionId);
  await popup.reload();
  let after;
  const recovered = Date.now() + 15000;
  while (true) {
    after = await status();
    metadata = JSON.parse(await readFile(path.join(before.path, 'session.json')));
    if (!after.recording && /interrupted/i.test(after.message) && metadata.state === 'interrupted') break;
    assert.ok(Date.now() < recovered, `worker interruption was not reported: ${JSON.stringify({ status: after, manifest: metadata })}`);
    await new Promise(resolve => setTimeout(resolve, 50));
  }
  assert.equal(after.path, before.path);
  assert.equal(await popup.evaluate(() => chrome.action.getBadgeText({})), '');
  assert.ok(metadata.tracks.some(track => track.frames > track.sampleRate * 4), 'audio accepted before worker death survived');
  const retained = await Promise.all(metadata.tracks.map(track => readFile(path.join(before.path, track.file))));
  assert.ok(retained.some(wav => wav.subarray(44).some(byte => byte !== 0)), 'the generated tone survived worker death');
  if (mode === 'tab') assert.equal(await popup.evaluate(async () => (await chrome.runtime.getContexts({ contextTypes: ['OFFSCREEN_DOCUMENT'] })).length), 0);
  return { path: before.path, status: after, manifest: metadata };
}
