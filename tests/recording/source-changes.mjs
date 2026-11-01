// UX11 through the actual loaded extension, Library dialog and private Pulse.
import assert from 'node:assert/strict';
import {spawn, execFileSync} from 'node:child_process';
import {createWriteStream} from 'node:fs';
import {readFile} from 'node:fs/promises';
import path from 'node:path';

export async function run({context, call, join, record, recording, samples, power, poll, automatic, id, artifacts, evidence}) {
 const {constants} = JSON.parse(await readFile(path.resolve(import.meta.dirname, '../../browser-protocol/conformance/multistream/lifecycle.json')));
 let producer;
 async function choose(directory, me, others) {
  await call({action: 'selection', path: directory, me, others});
  await poll(async () => {
   const status = await call();
   const active = status.statuses.find(s => s.path === directory && s.recording);
   const selected = active?.events?.filter(e => e.type === 'selection').at(-1);
   return !status.sourceChangePending && selected?.me === me && selected?.others === others;
  }, 'real source dialog applies choice');
  await call({action: 'sources-done'});
 }
 try {
  // A normal hang-up keeps the tab/companion reachable. It must save the
  // meeting after the call-end grace, without ever starting system capture.
  const hungUp = await join('Hang-up with tab still open', 770, 1100, id);
  await record();
  const hungUpDirectory = await recording('Hang-up with tab still open');
  await poll(async () => (await call()).meetings.some(m => m.title === 'Hang-up with tab still open' && m.heard),
   'remote audio evidence arms the call-end policy before hang-up');
  await hungUp.meeting.evaluate(() => meeting.pc.close());
  const savedHangup = await poll(async () => {
   const current = await samples(hungUpDirectory);
   evidence.hangup = {directory: hungUpDirectory, state: current.manifest.state,
    problem: current.manifest.problem, problemKind: current.manifest.problemKind,
    tracks: current.tracks.map(t => ({kind: t.kind, frames: t.frames, ended: t.ended}))};
   assert.ok(!current.tracks.some(t => t.kind === 'system'), 'hang-up never falls back while waiting');
   return current.manifest.state === 'completed' && current;
  }, 'hang-up saves after the shared others-gone grace while the tab remains open', constants.OTHERS_GONE_GRACE + 20000);
  assert.ok(!hungUp.meeting.isClosed());
  assert.ok(!savedHangup.tracks.some(t => t.kind === 'system'), 'hang-up never falls back');
  evidence.scenarios.push({name: 'hang-up-tab-open-stops-without-fallback', directory: hungUpDirectory});
  assert.equal(savedHangup.manifest.events.at(-1).reason, 'callEnded');
  const closed = await join('Browser-confirmed closed tab', 770, 1100, id);
  await record();
  const closedDirectory = await recording('Browser-confirmed closed tab');
  await closed.meeting.close();
  const savedClosed = await poll(async () => {
   const current = await samples(closedDirectory);
   return current.manifest.state === 'completed' && current;
  }, 'confirmed closed tab saves after call-end grace');
  assert.equal(savedClosed.manifest.events.at(-1).reason, 'tabClosed');
  assert.ok(!savedClosed.tracks.some(t => t.kind === 'system'), 'closed tab never starts fallback');
  evidence.scenarios.push({name: 'closed-tab-stops-with-end-reason-and-no-fallback', directory: closedDirectory});
  const first = await join('Fallback browser meeting', 440, 880, id);
  await join('Unaffected browser meeting', 550, 990, id);
  await record();
  const directory = await recording('Fallback browser meeting');
  const otherDirectory = await recording('Unaffected browser meeting');
  const initial = await samples(directory);
  const initialMic = initial.tracks.find(t => t.kind === 'microphone');
  const initialRemote = initial.tracks.find(t => t.kind === 'remote');
  await choose(directory, false, true);
  const frozenMic = (await samples(directory)).tracks.find(t => t.file === initialMic.file).frames;
  const otherBefore = (await samples(otherDirectory)).tracks.find(t => t.kind === 'microphone').frames;
  await poll(async () => (await samples(otherDirectory)).tracks.find(t => t.kind === 'microphone').frames >= otherBefore + 48000, 'other meeting microphone continues');
  assert.equal((await samples(directory)).tracks.find(t => t.file === initialMic.file).frames, frozenMic);
  await choose(directory, true, false);
  const frozenRemote = (await samples(directory)).tracks.find(t => t.file === initialRemote.file).frames;
  const micBefore = (await samples(directory)).tracks.findLast(t => t.kind === 'microphone' && !t.ended).frames;
  await poll(async () => (await samples(directory)).tracks.findLast(t => t.kind === 'microphone' && !t.ended).frames >= micBefore + 48000, 'Me continues while browser packets are discarded');
  assert.equal((await samples(directory)).tracks.find(t => t.file === initialRemote.file).frames, frozenRemote);
  await choose(directory, true, true);
  await poll(async () => (await samples(directory)).tracks.some(t => t.kind === 'remote' && t.file !== initialRemote.file && t.frames >= 96000), 'new browser span has two seconds');
  const resumed = await samples(directory);
  const remote = resumed.tracks.find(t => t.kind === 'remote' && t.file !== initialRemote.file);
  assert.equal(initialRemote.sourceTrackId, remote.sourceTrackId);
  assert.ok(remote.sourceTrackId && remote.id !== initialRemote.id && remote.startOffsetMs > initialRemote.startOffsetMs);
  const wanted = power(remote, 880), bleed = power(remote, 990);
  assert.ok(wanted > .001 && bleed < wanted * .01, JSON.stringify({wanted, bleed}));
  await automatic(true);
  await call({action: 'close-window'});
  execFileSync('pactl', ['--server', process.env.PULSE_SERVER, 'set-sink-mute', 'cassini_desktop', '0']);
  producer = spawn('/usr/bin/python3', ['-c', `import gi,signal\ngi.require_version('Gst','1.0')\nfrom gi.repository import Gst,GLib\nGst.init(None)\np=Gst.parse_launch('audiotestsrc is-live=true freq=1210 volume=0.25 ! audioconvert ! audioresample ! audio/x-raw,rate=48000,channels=1 ! pulsesink device=cassini_desktop sync=false')\nloop=GLib.MainLoop()\nsignal.signal(signal.SIGTERM,lambda *_:loop.quit())\np.set_state(Gst.State.PLAYING)\ntry:loop.run()\nfinally:p.set_state(Gst.State.NULL)\n`], {stdio: ['ignore', 'ignore', 'pipe']});
  producer.stderr.pipe(createWriteStream(path.join(artifacts, 'system-fallback-producer.log')));
  const beforeLoss = await samples(directory);
  const mic = beforeLoss.tracks.findLast(t => t.kind === 'microphone' && !t.ended);
  const independentBefore = (await samples(otherDirectory)).tracks.find(t => t.kind === 'remote').frames;
  await first.meeting.reload();
  const fallback = await poll(async () => {
   const current = await samples(directory);
   return current.tracks.some(t => t.kind === 'system' && t.frames >= 96000) && current;
  }, 'tab reload falls back to actual system audio');
  const event = fallback.manifest.events.filter(e => e.type === 'othersSource').at(-1);
  const system = fallback.tracks.find(t => t.kind === 'system');
  assert.equal(event.source, 'system'); assert.equal(event.reason, 'lost');
  assert.equal(system.startOffsetMs, event.atMs);
  assert.ok(fallback.tracks.find(t => t.file === mic.file).frames > mic.frames + 48000, 'microphone continues in the same WAV');
  assert.ok((await samples(otherDirectory)).tracks.find(t => t.kind === 'remote').frames > independentBefore + 48000);
  assert.ok(!(await samples(otherDirectory)).tracks.some(t => t.kind === 'system'), 'loss must affect only its own meeting');
  const systemWanted = power(system, 1210), microphoneBleed = power(system, 660);
  assert.ok(systemWanted > .001 && microphoneBleed < systemWanted * .01, JSON.stringify({systemWanted, microphoneBleed}));
  const notification = await poll(async () => (await call()).notifications.recording, 'fallback hidden notification');
  assert.ok(notification.body.includes('Recording the others from this computer’s sound.'));
  // D33: the same tab returns on its retained native connection. It must
  // restore browser PCM, keeping the folder and the original microphone WAV.
  await first.meeting.click('#join');
  await first.meeting.evaluate(async () => {document.title = 'Fallback browser meeting'; await window.join(440);});
  await first.remote.evaluate(async () => {meeting.pc.close(); await window.join(880);});
  const offer = await first.meeting.evaluate(async () => {
   await meeting.pc.setLocalDescription(await meeting.pc.createOffer()); return meeting.description();
  });
  const answer = await first.remote.evaluate(async offer => {
   await meeting.pc.setRemoteDescription(offer);
   await meeting.pc.setLocalDescription(await meeting.pc.createAnswer()); return meeting.description();
  }, offer);
  await first.meeting.evaluate(answer => meeting.pc.setRemoteDescription(answer), answer);
  const returned = await poll(async () => {
   const current = await samples(directory);
   return current.manifest.events.some(e => e.reason === 'restored') &&
    current.tracks.some(t => t.kind === 'remote' && !t.ended && t.frames >= 96000) && current;
  }, 'same-tab browser return restores a fresh span in the original folder');
  const returnedRemote = returned.tracks.find(t => t.kind === 'remote' && !t.ended);
  const restoredWanted = power(returnedRemote, 880), systemBleed = power(returnedRemote, 1210);
  assert.ok(restoredWanted > .001 && systemBleed < restoredWanted * .01,
   JSON.stringify({restoredWanted, systemBleed}));
  assert.equal(returned.manifest.state, 'recording');
  assert.ok(returned.tracks.find(t => t.file === mic.file && !t.ended));
  assert.ok(returned.tracks.find(t => t.file === system.file).ended);
  assert.equal((await call()).statuses.filter(s => s.path === directory && s.recording).length, 1);
  assert.ok((await samples(otherDirectory)).tracks.find(t => t.kind === 'remote').frames > independentBefore + 96000);
  evidence.scenarios.push({name: 'same-tab-return-restores-browser-in-same-folder', directory,
   originalMic: mic.file, restoredWanted, systemBleed});
  // Lose the page again before the separate worker-recovery/expiry proof.
  await first.meeting.reload();
  await poll(async () => {
   const current = await samples(directory);
   return current.tracks.filter(t => t.kind === 'system').length === 2 &&
    current.manifest.events.filter(e => e.type === 'othersSource').at(-1).source === 'system';
  }, 'second reload creates its own fallback span');
  // A worker restart loses every browser audio connection. The fallback that
  // already exists must be rebound, never duplicated or switched back.
  const oldIds = new Set((await call()).statuses.filter(s => s.recording).map(s => s.id));
  const lifecycle = await context.newCDPSession(first.meeting);
  await lifecycle.send('ServiceWorker.enable');
  await lifecycle.send('ServiceWorker.stopAllWorkers');
  await lifecycle.detach();
  // A new content-script connection wakes the terminated MV3 worker. Merely
  // clicking the fixture's Join button does not send an extension event.
  await first.meeting.reload();
  await poll(async () => {
   const current = await call();
   const active = current.statuses.filter(s => s.recording);
   return current.connected > 0 && active.length === 2 && active.every(s => oldIds.has(s.id) && [directory, otherDirectory].includes(s.path));
  }, 'worker reconnect retains exactly the same two capture folders');
  const afterRestart = await samples(directory);
  assert.equal(afterRestart.manifest.events.filter(e => e.type === 'othersSource').at(-1).source, 'system');
  assert.equal(afterRestart.tracks.filter(t => t.kind === 'system').length, 2);
  await poll(async () => (await samples(directory)).manifest.state === 'completed',
   'fallback saves when no active call returns within two minutes', 135000);
  assert.equal((await samples(directory)).manifest.events.at(-1).reason, 'pageGone');
  assert.equal((await samples(otherDirectory)).manifest.state, 'recording');
  evidence.scenarios.push({name: 'reload-fallback-without-return-stops-at-bounded-grace', directory});
  await call({action: 'notification-stop'});
  await poll(async () => !(await call()).statuses.some(s => s.recording), 'hidden Stop saves all fallbacks');
  assert.equal((await samples(directory)).manifest.state, 'completed');
  assert.equal((await samples(otherDirectory)).manifest.state, 'completed');
  await call({action: 'reopen'});
  await automatic(false);
  evidence.scenarios.push({name: 'source-dialog-off-on-tab-loss-native-fallback-hidden-worker-recovery-no-duplicates', directory, otherDirectory, wanted, bleed, systemWanted, microphoneBleed});
 } catch (error) {
  evidence.sourceFailure = await call().catch(failure => ({error: failure.message}));
  throw error;
 } finally {producer?.kill();}
}
