// UX11 through the actual loaded extension, Library dialog and private Pulse.
import assert from 'node:assert/strict';
import {spawn, execFileSync} from 'node:child_process';
import {createWriteStream} from 'node:fs';
import path from 'node:path';

export async function run({context, call, join, recording, samples, power, poll, automatic, id, artifacts, evidence}) {
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
  const first = await join('Fallback browser meeting', 440, 880, id);
  await join('Unaffected browser meeting', 550, 990, id);
  await call({action: 'record'});
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
   return current.connected > 0 && active.length === 2 && active.every(s => !oldIds.has(s.id) && [directory, otherDirectory].includes(s.path));
  }, 'worker reconnect retains exactly the same two capture folders');
  const afterRestart = await samples(directory);
  assert.equal(afterRestart.manifest.events.filter(e => e.type === 'othersSource').at(-1).source, 'system');
  assert.equal(afterRestart.tracks.filter(t => t.kind === 'system').length, 1);
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
