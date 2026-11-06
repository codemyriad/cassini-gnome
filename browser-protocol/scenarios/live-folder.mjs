// Shared test 2 driver (D68): one live recording, through an app's real writer, of a call
// with a 60 s remote mute, a late remote, a connected remote that never sends, the app's
// microphone switched off and back on at 90 s, and a forced resume (SIGKILL of the
// session's audio host). The folder is then judged only by conformance/live-folder/check.py.
//
// Both apps run this same driver; each supplies an adapter (`app`) for its own actions:
//   record()            start recording the meeting the way a person does (async, returns once asked)
//   selectMe(on)        switch the app's Me (microphone) selection for this recording
//   snapshot()          {path, captureId} of this recording, or null before it exists
//   meetingRow()        the app's row for the meeting tab: {sourceCount, heard}, or null
//   stop()              stop this recording the way a person does (returns once asked)
//   audioHostPids()     live pids of the audio native hosts (scenarios/native-launcher.mjs)
//   folders()           names of the Recording-* folders in the app's Library
//   controlHost         the app's production control host (io.codemyriad.cassini.control) as
//                       writeNativeHost({..., observe}) registered it (scenarios/native-launcher.mjs)
//   browserPid()        pid of the browser process that runs the extension
//   saved?(path)        optional: wait for app-owned files written after Stop (bounded)
//   cleanup({failed})   end everything the adapter started for the run (the app, its
//                       recording, the browsers and pages, its relays, broker and hosts),
//                       leaving no process. Idempotent; the driver awaits it exactly once,
//                       in a finally, bounded by CLEANUP_MS, after a failure's evidence is
//                       written and after check.py ran (or was killed at CHECK_MS). It leaves
//                       controlHost.observe in place.
//   name, protocolCommit, nativeTones: {microphone: Hz}, startsWithMeOff?: true
// meeting is the call tab (in the browser with the extension), remote its far end, worker
// the extension's service worker. Every wait polls the folder (scenarios/folder-clock.mjs)
// or the app, with a labelled bound from scenario.json; nothing sleeps (E1). The recording's
// identity (captureId, timelineOriginMs) is the extension's cassiniSessions[] entry for the
// meeting tab; every resume request is read off the control pipe by the launcher's relay
// (scenario.json evidence.resumeObservation), never from the app.
//
// Teardown (every path, in a finally): if Record was asked and Stop was not, the driver
// asks stop() (bounded by the stop bound); then it awaits cleanup(); then it ends any
// control relay still running (stopRelays) and any control launcher still waiting in open()
// on its FIFO (stopLaunchers), which neither the app nor the browser's exit would end.
// out/teardown.json says what each step did; on a passing run a failed step fails the run.
import assert from 'node:assert/strict';
import { execFile } from 'node:child_process';
import { readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { connectReceiveSlots, muteSlot, switchReceiveSlot } from './product-topologies.mjs';
import { eventsOf, extent, folderClock, poll, tracksOf } from './folder-clock.mjs';
import { monotonic, sessionClock, wallPair } from './session-clock.mjs';
import { controlConnections, RELAY_START_MS, resumeRequests, stopLaunchers, stopRelays, verifyControlPipe } from './native-launcher.mjs';

const ORACLE = path.resolve(import.meta.dirname, '../conformance/live-folder');
// Bound on the adapter's cleanup(): quitting the app, closing two browsers, ending hosts.
export const CLEANUP_MS = 60000;
// Bound on check.py: one that does not finish is killed and fails the run (then torn down).
export const CHECK_MS = 300000;
// While Record runs, the wall clock is read this often (a reading, never a wait), so the origin the
// extension takes then lies between two readings this close: a wall-clock step there is told from
// a slew, and never interpolated across (scenarios/session-clock.mjs).
export const ORIGIN_SAMPLE_MS = 10;

// promise, or a rejection naming label once ms pass (the timer never outlives it).
function bounded(promise, ms, label) {
  let timer;
  const late = new Promise((_, reject) => { timer = setTimeout(() => reject(new Error(`Timed out after ${ms} ms: ${label}`)), ms); });
  return Promise.race([Promise.resolve(promise), late]).finally(() => clearTimeout(timer));
}

// scenario.json's constants plus the corpus constants it names (read, never copied).
export async function loadScenario(oracle = ORACLE) {
  const scenario = JSON.parse(await readFile(path.join(oracle, 'scenario.json'), 'utf8'));
  const corpus = JSON.parse(await readFile(path.join(oracle, scenario.corpus.file), 'utf8')).constants;
  const constants = { ...scenario.constants };
  for (const name of scenario.corpus.constants) constants[name] = corpus[name];
  return { scenario, constants, bounds: scenario.driver.boundsMs, pollMs: scenario.driver.pollMs, oracle };
}

const remoteTracks = manifest => tracksOf(manifest, track => track.kind === 'remote' && typeof track.sourceTrackId === 'string');
const others = manifest => eventsOf(manifest, 'othersSource');
const lengthMs = track => (extent(track) ? extent(track)[1] - extent(track)[0] : 0);

// One receive slot's inbound-rtp counters on the meeting page (zeros before any packet).
// Diagnostic only: the jitter buffer's mean delay between two samples is how long the
// receiver still plays audio the sender has already stopped.
async function receiverStats(meeting, slot) {
  return meeting.evaluate(async slot => {
    const receiver = topologyPeer.getTransceivers()[slot].receiver;
    const inbound = [...(await receiver.getStats()).values()].find(r => r.type === 'inbound-rtp') || {};
    const pick = ['packetsReceived', 'jitterBufferDelay', 'jitterBufferEmittedCount', 'jitterBufferTargetDelay', 'concealedSamples', 'totalSamplesReceived'];
    return { connectionState: topologyPeer.connectionState, ...Object.fromEntries(pick.map(name => [name, inbound[name] ?? 0])) };
  }, slot);
}
const playoutMs = (from, to) => to.jitterBufferEmittedCount > from.jitterBufferEmittedCount
  ? Math.round(1000 * (to.jitterBufferDelay - from.jitterBufferDelay) / (to.jitterBufferEmittedCount - from.jitterBufferEmittedCount)) : null;

// check.py on the folder and evidence, its result written to result. Resolves {code, stdout,
// stderr} once it exits; rejects, after killing it, if it has not exited within ms.
export function runOracle({ python = 'python3', oracle = ORACLE, folder, evidence, result, ms = CHECK_MS }) {
  return new Promise((resolve, reject) => execFile(python, ['-I', path.join(oracle, 'check.py'), 'check', folder,
    '--evidence', evidence, '--out', result], { maxBuffer: 16 << 20, timeout: ms, killSignal: 'SIGKILL' }, (error, stdout, stderr) => {
    if (error?.code === 'ERR_CHILD_PROCESS_STDIO_MAXBUFFER') reject(new Error(`check.py: ${error.message}`));
    else if (error?.killed) reject(new Error(`check.py did not finish within ${ms} ms (killed)\n${stdout}${stderr}`));
    else resolve({ code: error ? (typeof error.code === 'number' ? error.code : 2) : 0, stdout, stderr });
  }));
}

// relayStartMs, cleanupMs and checkMs are the control relay's start bound, cleanup()'s bound
// and check.py's (tests shorten them).
// muteHold: the spans of `key` that were open when the mute began and have ended since. A span
// that had already ended before the mute (an earlier interruption, which the oracle judges) is
// not one: Mac #153 reported such a span as "A ended during the mute".
export const endedDuringMute = (manifest, key, openAtMute) =>
  tracksOf(manifest, key).filter(t => t.ended === true && openAtMute.has(t.id)).map(t => t.id);

export async function runLiveFolder({ meeting, remote, app, worker, out, log = console.log, python = 'python3',
  relayStartMs = RELAY_START_MS, cleanupMs = CLEANUP_MS, checkMs = CHECK_MS, clocks = {} }) {
  let failed = true, recordAsked = false, stopAsked = false;
  try {
    const value = await scenario();
    failed = false;
    return value;
  } finally {
    await teardown();
  }

  // Ends the run whatever happened: see the header. A step's failure never skips the next.
  async function teardown() {
    const report = { failed }, problems = [];
    const attempt = async (name, run) => {
      try { report[name] = (await run()) ?? 'done'; }
      catch (error) { report[name] = { error: String(error?.stack || error) }; problems.push(`${name}: ${error?.message || error}`); }
    };
    if (recordAsked && !stopAsked) await attempt('stop', async () => {
      const { bounds } = await loadScenario();
      await bounded(app.stop(), bounds.stop, 'teardown: stop()');
    });
    await attempt('cleanup', () => (typeof app?.cleanup === 'function'
      ? bounded(app.cleanup({ failed }), cleanupMs, 'teardown: cleanup()')
      : Promise.reject(new Error('the adapter has no cleanup()'))));
    if (app?.controlHost?.observe) {
      await attempt('relaysStopped', () => stopRelays(app.controlHost));
      await attempt('launchersKilled', () => stopLaunchers(app.controlHost));
    }
    if (problems.length) log(`live-folder: teardown: ${problems.join('; ')}`);
    try { await writeFile(path.join(out, 'teardown.json'), JSON.stringify(report, null, 1) + '\n'); } catch {}
    if (problems.length && !failed) throw new Error(`teardown: ${problems.join('; ')}`);
  }

  async function scenario() {
    const { constants: C, bounds, pollMs, oracle } = await loadScenario();
    // Every requested and observed time is a monotonic reading (scenarios/session-clock.mjs), mapped
    // onto the session timeline once the extension's origin is known: the apps' timeline too.
    // clocks ({mono, wall}) replaces both readings in the driver's tests only.
    const now = clocks.mono ?? monotonic;
    // Where the wall clock stood, read from before Record until the extension reports the origin,
    // so sessionClock maps the origin at its own instant (scenarios/session-clock.mjs).
    const pairs = [], sample = () => { pairs.push(wallPair(clocks)); };
    let sampling = null;
    const stopSampling = () => { clearInterval(sampling); sampling = null; };
    const actions = {}, notes = { steps: [] };
    let origin = null, timeline = null, clock = null, captureId = null, maxSourceCount = 0, slotC = null, before = null, newFolders = null;
    let requests = null, browserPid = null;
    const session = at => timeline.at(at);
    // Every control host the browser's child, every frame readable (resumeObservation.process, unusable).
    // notes.controlParents keeps, per check, each connection's launch parent and its parent then
    // (null once exited), so the parent-pid proof can be read back from the evidence.
    const pipe = async label => {
      const connections = await verifyControlPipe(app.controlHost, browserPid, label, { relayStartMs });
      (notes.controlParents ??= {})[label] = connections.map(c => ({ connection: c.connection, pid: c.pid, launchParent: c.launchParent, parent: c.parent }));
      return connections;
    };
    const step = (name, requested, observed) => {
      actions[name] = { requested, observed };
      const shown = at => (timeline === null ? '-' : Math.round(session(at)));
      log(`live-folder: ${name} requested ${shown(requested)} observed ${shown(observed)} (session ms)`);
    };
    const row = async () => {
      const value = await app.meetingRow();
      if (Number.isInteger(value?.sourceCount)) maxSourceCount = Math.max(maxSourceCount, value.sourceCount);
      return value;
    };
    // Wait until the folder clock reaches target (session ms): the clock runs in real time,
    // so the bound is the distance left plus the record bound for checkpoint lag.
    const untilS = (target, label) => clock.waitFolder(() => clock.S() >= target, `${label}: folder clock reaches ${target} ms`,
      Math.max(0, target - clock.S()) + bounds.record);
    const evidence = () => ({
      scenario: 'live-folder/1', app: app.name, protocolCommit: app.protocolCommit,
      extensionVersion: notes.extensionVersion, newFolders,
      actions: Object.fromEntries(Object.entries(actions).map(([name, { requested, observed }]) =>
        [name, { requestedAtMs: session(requested), observedAtMs: session(observed) }])),
      slotC, maxSourceCount, nativeTones: app.nativeTones, captureId, timelineOriginMs: origin, resumeRequests: requests, notes,
    });

    try {
      notes.extensionVersion = await worker.evaluate(() => chrome.runtime.getManifest().version);
      assert.ok(app.controlHost?.observe, 'setup: the adapter registers its control host through the observing launcher (observe)');
      browserPid = await app.browserPid();
      assert.ok(Number.isInteger(browserPid) && browserPid > 1, `setup: the browser pid, got ${browserPid}`);
      notes.browserPid = browserPid;  // what every control host's parent is checked against
      // The meeting tab, as the extension sees it: its cassiniSessions[] entry names the recording.
      const tabs = await worker.evaluate(async url => (await chrome.tabs.query({})).filter(t => t.url === url).map(t => t.id), meeting.url());
      assert.equal(tabs.length, 1, `setup: exactly one tab shows the meeting ${meeting.url()}, found ${JSON.stringify(tabs)}`);
      const [meetingTab] = tabs;
      notes.meetingTab = meetingTab;
      // 0. Setup: three recvonly slots on one peer. A sends from the start, B is pre-negotiated
      // and sends later, C stays connected and never gets a track. The page plays the call, as
      // a meeting page does, so A's receiver plays out about 50 ms behind the sender; an
      // unplayed receiver buffers until the capture pulls it and then lags by a second or
      // more, which would carry A's tone past the mute's edge.
      await connectReceiveSlots(meeting, remote, [[C.TONE_A_HZ], null, null], { play: true });
      await poll(async () => { const value = await row(); return value?.sourceCount === 3 && value.heard === true; },
        'setup: the app lists the call with sourceCount 3 and heard', bounds.setup, { pollMs });
      before = await app.folders();
      await poll(async () => (await pipe('setup')).some(c => c.pid !== null),
        'setup: the browser started a control host through the observing launcher', bounds.setup, { pollMs });

      // 1. Record.
      sample();
      sampling = setInterval(sample, ORIGIN_SAMPLE_MS);
      let requested = now();
      recordAsked = true;
      await app.record();
      sample();
      const status = await poll(async () => { sample(); const value = await app.snapshot(); return value?.path && value.captureId ? value : null; },
        'record: the app reports the recording folder and capture id', bounds.record, { pollMs });
      notes.appCaptureId = status.captureId;
      clock = folderClock(status.path, { pollMs });
      const keyA = await clock.waitFolder(manifest => {
        sample();
        const track = remoteTracks(manifest).find(t => lengthMs(t) >= 1000);
        return track?.sourceTrackId;
      }, 'record: A has 1000 ms of frames', bounds.record);
      const recorded = now();
      // The recording's identity and timeline origin, as the extension's service worker holds
      // them for the meeting tab: app-neutral, and what a resume request must name.
      const entry = await poll(() => { sample(); return worker.evaluate(async tab => {
        const { cassiniSessions = [] } = await chrome.storage.session.get('cassiniSessions');
        const found = cassiniSessions.filter(s => s.tabId === tab);
        return found.length ? found : null;
      }, meetingTab); }, 'record: the extension holds a cassiniSessions entry for the meeting tab', bounds.record, { pollMs });
      assert.equal(entry.length, 1, `record: one cassiniSessions entry for the meeting tab, found ${JSON.stringify(entry)}`);
      assert.ok(typeof entry[0].captureId === 'string' && entry[0].captureId, `record: the entry's captureId ${JSON.stringify(entry[0])}`);
      assert.ok(Number.isSafeInteger(entry[0].startedAt) && entry[0].startedAt >= 0, `record: the entry's startedAt ${JSON.stringify(entry[0])}`);
      ({ captureId, startedAt: origin } = entry[0]);
      // SPEC: the wall-clock origin is mapped onto the monotonic clock once, at its own instant
      // (the pairs read since before Record bracket it); never the wall clock again.
      stopSampling();
      timeline = sessionClock(origin, { pairs, ...clocks });
      notes.originPairs = pairs.length;
      if (notes.appCaptureId !== captureId) log(`live-folder: the app reports captureId ${notes.appCaptureId}, the extension ${captureId}`);
      step('record', requested, recorded);
      await pipe('record');
      notes.keyA = keyA;
      const receiverAtRecord = await receiverStats(meeting, 0);
      await row();

      // 2. Me off (skipped when the app starts with Me off: the start selection is the off one).
      if (app.startsWithMeOff) {
        actions.meOff = { ...actions.record };
      } else {
        requested = now();
        await app.selectMe(false);
        await clock.waitFolder(manifest => eventsOf(manifest, 'selection').at(-1)?.me === false,
          'meOff: the last selection event shows me:false', bounds.meOff);
        step('meOff', requested, now());
      }

      // 3. Mute A once F(A) >= MUTE_START_MS and the session clock >= MUTE_START_MS: F(A) may
      // lead the session clock (D61 output-time stamps), so both. A is then the only open source.
      await clock.waitFolder(() => clock.F(keyA) >= C.MUTE_START_MS && timeline.now() >= C.MUTE_START_MS,
        `mute: F(A) and the session clock reach ${C.MUTE_START_MS} ms`, C.MUTE_START_MS + bounds.record);
      const spansA = new Set(tracksOf(clock.manifest, keyA).map(t => t.id)), othersBefore = others(clock.manifest).length;
      const openA = new Set(tracksOf(clock.manifest, keyA).filter(t => t.ended !== true).map(t => t.id));
      notes.endedBeforeMute = [...spansA].filter(id => !openA.has(id));
      const receiverAtMute = await receiverStats(meeting, 0);
      notes.receiverA = { atRecord: receiverAtRecord, atMute: receiverAtMute, playoutMsBeforeMute: playoutMs(receiverAtRecord, receiverAtMute) };
      requested = now();
      await muteSlot(remote, 0);
      step('mute', requested, now());
      log(`live-folder: A's mean jitter-buffer delay before the mute ${notes.receiverA.playoutMsBeforeMute} ms`);

      // 4. Hold the mute for MUTE_MS of A's own frames: one span, no source change, never ended.
      // Stalled frames fail the bound: that stall is what this step is here to catch.
      const muteAt = session(actions.mute.requested);
      await clock.waitFolder(manifest => {
        const ids = tracksOf(manifest, keyA).map(t => t.id);
        assert.deepEqual(ids.filter(id => !spansA.has(id)), [], `muteHold: A opened a new span during the mute`);
        assert.equal(others(manifest).length, othersBefore, `muteHold: an othersSource event during the mute: ${JSON.stringify(others(manifest).at(-1))}`);
        assert.deepEqual(endedDuringMute(manifest, keyA, openA), [], 'muteHold: A ended during the mute');
        return clock.F(keyA) >= muteAt + C.MUTE_MS && timeline.now() >= muteAt + C.MUTE_MS;
      }, `muteHold: F(A) and the session clock reach mute + ${C.MUTE_MS} ms`, bounds.muteHold);
      requested = now();
      const unmutedFrom = clock.F(keyA);
      await switchReceiveSlot(remote, 0, [C.TONE_A_HZ]);
      await clock.waitFolder(() => clock.F(keyA) >= unmutedFrom + C.PROOF_MS, `unmute: F(A) advances ${C.PROOF_MS} ms`, bounds.unmute);
      step('unmute', requested, now());

      // 5. Late remote: B starts sending on its pre-negotiated slot.
      await untilS(C.LATE_REMOTE_MS, 'lateRemote');
      requested = now();
      await switchReceiveSlot(remote, 1, [C.TONE_B_HZ]);
      const keyB = await clock.waitFolder(manifest => remoteTracks(manifest).find(t => t.sourceTrackId !== keyA && lengthMs(t) >= C.PROOF_MS)?.sourceTrackId,
        `lateRemote: a new sourceTrackId with ${C.PROOF_MS} ms of frames`, bounds.lateRemote);
      step('lateRemote', requested, now());
      notes.keyB = keyB;

      // 6. Me on at MIC_ON_MS. The microphone span starts at the me:true selection's atMs
      // (a late first buffer is a prefix gap, inside the span), so wait for that span.
      await untilS(C.MIC_ON_MS, 'meOn');
      requested = now();
      await app.selectMe(true);
      await clock.waitFolder(manifest => {
        const on = eventsOf(manifest, 'selection').at(-1);
        if (on?.me !== true || !Number.isFinite(on.atMs)) return false;
        return tracksOf(manifest, track => track.kind === 'microphone')
          .some(t => extent(t) && extent(t)[0] >= on.atMs - 1 && lengthMs(t) >= C.PROOF_MS);
      }, `meOn: selection me:true and a microphone span from it with ${C.PROOF_MS} ms`, bounds.meOn);
      step('meOn', requested, now());

      // 7. Forced resume: SIGKILL the session's one audio host. The app commits its fallback
      // (system/lost) first; only then may the resume complete (browser/restored), so the
      // folder always has the committed-loss form (D68 item 1).
      await untilS(C.KILL_AT_MS, 'kill');
      await pipe('kill');
      const pids = await app.audioHostPids();
      assert.equal(pids.length, 1, `kill: exactly one live audio host, found ${JSON.stringify(pids)}`);
      const folderCount = (await app.folders()).length;
      const spansBefore = new Set(remoteTracks(clock.manifest).map(t => t.id));
      const othersAtKill = others(clock.manifest).length;
      notes.killedPid = pids[0];
      requested = now();
      process.kill(pids[0], 'SIGKILL');
      const lost = await clock.waitFolder(manifest => {
        const after = others(manifest).slice(othersAtKill);
        const lostAt = after.findIndex(e => e.source === 'system' && e.reason === 'lost');
        const restoredAt = after.findIndex(e => e.source === 'browser' && e.reason === 'restored');
        assert.ok(restoredAt < 0 || lostAt >= 0 && lostAt < restoredAt,
          `kill: browser/restored before any system/lost: ${JSON.stringify(after)}`);
        return lostAt >= 0 ? after[lostAt] : null;
      }, 'kill: othersSource system/lost', bounds.lost);
      step('kill', requested, now());
      requested = actions.kill.observed;
      await clock.waitFolder(manifest => {
        const restored = others(manifest).some(e => e.source === 'browser' && e.reason === 'restored' && e.atMs >= lost.atMs);
        const fresh = key => remoteTracks(manifest).some(t => t.sourceTrackId === key && !spansBefore.has(t.id) && lengthMs(t) >= C.PROOF_MS);
        return restored && fresh(keyA) && fresh(keyB);
      }, `restore: browser/restored and new spans under A and B with ${C.PROOF_MS} ms each`, bounds.restore);
      step('restore', requested, now());
      await pipe('restore');
      assert.equal((await app.folders()).length, folderCount, 'restore: the resume made another Recording-* folder');
      notes.audioHostsAfterResume = await app.audioHostPids();

      // 8. Stop, after STOP_AFTER_RESTORE_MS more. Slot C's stats are the page-side evidence
      // that a connected remote with no packets existed (P4).
      await untilS(session(actions.restore.observed) + C.STOP_AFTER_RESTORE_MS, 'stop');
      const stats = await receiverStats(meeting, 2);
      slotC = { connectionState: stats.connectionState, packetsReceived: stats.packetsReceived };
      await row();
      requested = now();
      stopAsked = true;
      await app.stop();
      await clock.waitFolder(manifest => manifest.state !== 'recording' && eventsOf(manifest).at(-1)?.type === 'end',
        'stop: the folder leaves recording and ends with an end event', bounds.stop);
      step('stop', requested, now());
      // Every resume request up to the stop observation, reconnects included. A relay logs a
      // frame before forwarding it, so none the extension acted on is missing here.
      const connections = await pipe('stop');
      requests = resumeRequests(connections, timeline.originMono, actions.stop.observed);
      notes.controlConnections = connections.map(c => ({ connection: c.connection, pid: c.pid, launchParent: c.launchParent,
        live: c.live, parent: c.parent, frames: c.frames.length, resumes: c.resumes.length,
        ...(c.outputError ? { outputError: c.outputError } : {}) }));
      log(`live-folder: ${requests.length} resume request(s) on the control pipe: ${JSON.stringify(requests)}`);
      if (app.saved) await app.saved(status.path);
      const after = await app.folders();
      newFolders = after.filter(name => !before.includes(name));

      const evidenceFile = path.join(out, 'evidence.json'), resultFile = path.join(out, 'result.json');
      await writeFile(evidenceFile, JSON.stringify(evidence(), null, 1) + '\n');
      // The oracle, before the app quits or compacts the folder; bounded (a hang fails the run).
      const run = await runOracle({ python, oracle, folder: status.path, evidence: evidenceFile, result: resultFile, ms: checkMs });
      await writeFile(path.join(out, 'check.log'), run.stdout + run.stderr);
      log(run.stdout.trim().split('\n').at(-1) || run.stderr.trim());
      let result = null;
      try { result = JSON.parse(await readFile(resultFile, 'utf8')); } catch (error) { if (error.code !== 'ENOENT') throw error; }
      return { code: run.code, result, folder: status.path, evidence: evidenceFile, stdout: run.stdout, stderr: run.stderr };
    } catch (error) {
      stopSampling();
      // Whatever was reached, for the failure's evidence; check.py never sees a partial run.
      notes.failure = error.stack;
      notes.folder = clock?.describe();
      try { notes.controlConnections ??= (await controlConnections(app.controlHost)).map(({ frames, ...c }) => ({ ...c, frames: frames.length })); } catch {}
      try { await writeFile(path.join(out, 'evidence.partial.json'), JSON.stringify({ ...evidence(), actions: timeline === null ? actions : evidence().actions }, null, 1) + '\n'); } catch {}
      throw error;
    }
  }
}
