// SPDX-License-Identifier: GPL-3.0-or-later
// Real Talk split-PC producer -> actual GNOME auto Start -> private native PCM.
import assert from 'node:assert/strict';
import {joinNextcloudTalk, talkMediaState} from '../../browser-protocol/scenarios/nextcloud-talk.mjs';
import {installRtcTopologyProbe, summarizeRtcTopology, waitRtcTopology} from '../../browser-protocol/tests/rtc-topology-probe.mjs';

export async function run({context, remoteBrowser, call, automatic, poll, samples, evidence}) {
 const url = process.env.CASSINI_TEST_TALK_URL;
 assert.ok(['127.0.0.1', 'localhost', '[::1]'].includes(new URL(url).hostname), 'disposable local Talk room required');
 const meeting = await context.newPage(), participants = [];
 await meeting.addInitScript(installRtcTopologyProbe);
 try {
  await automatic(true);
  await joinNextcloudTalk(meeting, {url, name: 'Cassini automatic receiver', muted: true});
  for (const name of ['Cassini remote one', 'Cassini remote two']) {
   const guest = await remoteBrowser.newContext({permissions: ['microphone', 'camera']});
   participants.push(guest);
   await joinNextcloudTalk(await guest.newPage(), {url, name});
  }
  await poll(async () => (await talkMediaState(meeting)).remoteAudio >= 2, 'real Talk receives both remote peers');
  const row = await poll(async () => (await call()).meetings.find(row => row.url?.includes(new URL(url).pathname) && row.heard && row.sourceCount >= 2), 'truthful real Talk RTP reaches GNOME');
  const negative = await poll(async () => {
   const state = await call();
   return state.callSignals.flatMap(peer => peer.tabs).find(tab => tab.tabId === row.tabId && tab.callEvidence.state === 'unknown') && state;
  }, 'listen-only Talk has current unverified evidence');
  assert.ok(!negative.statuses.some(status => status.recording || status.starting), 'listen-only Talk must not automatically Start');
  assert.equal(negative.recordStartPending, false);
  await meeting.getByRole('button', {name: /^Unmute(?: audio)?(?: \(.*\))?$/i}).first().click();
  const before = await waitRtcTopology(meeting, {sending: 'captured'});
  const topology = summarizeRtcTopology(before, await waitRtcTopology(meeting, {previous: before, sending: 'captured'}));
  assert.ok(topology.sendingPeers > 0 && topology.receivingPeers > 0);
  assert.equal(topology.samePeerConnection, false, 'the actual Talk fixture must exercise split PCs');
  const admitted = await poll(async () => {
   const state = await call();
   const owner = state.callSignals.find(peer => peer.tabs.some(tab => tab.tabId === row.tabId && tab.callEvidence.state === 'active'));
   const signal = owner?.tabs.find(tab => tab.tabId === row.tabId);
   const inventory = state.meetings.find(meeting => meeting.tabId === row.tabId);
   const capture = state.statuses.find(status => status.tabId === row.tabId && status.recording && status.automatic && status.path);
   return signal && inventory && capture && {state, signal, inventory, capture};
  }, 'real producer evidence automatically starts Talk in GNOME');
  assert.equal(admitted.signal.remoteAudio, 'separated');
  assert.equal(admitted.signal.callEvidence.adapter.version, '2');
  assert.equal(admitted.inventory.browserDocumentId, admitted.signal.documentGeneration);
  assert.equal(admitted.inventory.documentGeneration, admitted.signal.sourceDocumentGeneration);
  assert.equal(admitted.inventory.callGeneration, admitted.signal.callEvidence.callGeneration);
  assert.ok(admitted.state.browserPeers.some(peer => peer.automaticVerified && peer.diagnostics.extensionVersion === '0.2.26'));
  const directory = admitted.capture.path;
  const audio = await poll(async () => {
   const audio = await samples(directory);
   return audio.tracks.filter(track => track.kind === 'remote' && track.frames >= track.sampleRate * 2).length === 2 && audio;
  }, 'automatic Talk records both remote PCM tracks');
  assert.equal(new Set(audio.tracks.filter(track => track.kind === 'remote').map(track => track.sourceTrackId)).size, 2);
  assert.ok(audio.tracks.filter(track => track.kind === 'remote').every(track => track.bytes.subarray(44).some(byte => byte !== 0)));
  const collector = await meeting.evaluate(() => {
   const value = __cassiniCallEvidenceV1.read();
   return {version: value.version, cohortQualified: value.cohortQualified, samePeerCount: value.samePeerCount};
  });
  assert.deepEqual(collector, {version: 2, cohortQualified: true, samePeerCount: 0});
  await call({action: 'stop-all'});
  const saved = await poll(async () => {
   const audio = await samples(directory);
   return audio.manifest.state !== 'recording' && audio;
  }, 'user Stop finalizes automatically recorded Talk');
  assert.equal(saved.manifest.state, 'completed');
  assert.equal(saved.manifest.events.at(-1).reason, 'user');
  evidence.scenarios.push({name: 'real Talk split-PC: listen-only negative, verified automatic Start and separate remote PCM', topology, collector, directory, signal: admitted.signal});
 } finally {
  await automatic(false);
  for (const guest of participants) await guest.close();
  await meeting.close();
 }
}
