// Deterministic PR replay of privacy-safe platform topology fixtures
// (tests/fixtures/platforms/*.json) plus synthetic WebAudio negative control.
// Uses local loopback WebRTC, fake media devices and the private test hosts;
// never live accounts, credentials or external network endpoints.
import assert from 'node:assert/strict';
import { createServer as createHttpServer } from 'node:http';
import { createServer as createNetServer } from 'node:net';
import { createHash, generateKeyPairSync } from 'node:crypto';
import { watch } from 'node:fs';
import { chmod, cp, mkdir, mkdtemp, readdir, readFile, rm, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { tonePower } from '../scenarios/tones.mjs';
import { eventValue } from './support/event-value.mjs';
import {
  dumpPlatformTopologyFixture,
  installRtcTopologyProbe,
  summarizeRtcTopology,
  validatePlatformTopologyFixture,
  waitRtcTopology
} from './rtc-topology-probe.mjs';

const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const fixturesDir = path.join(root, 'tests/fixtures/platforms');

const fixtureFiles = (await readdir(fixturesDir)).filter(name => name.endsWith('.json')).sort();
assert.ok(fixtureFiles.includes('ms-teams-2026-11-12.json'), 'MS Teams fixture must be present');

const fixtures = await Promise.all(fixtureFiles.map(async file => {
  const parsed = JSON.parse(await readFile(path.join(fixturesDir, file), 'utf8'));
  return { file, fixture: validatePlatformTopologyFixture(parsed) };
}));

function expectedAutoAdmission(fixture) {
  const hasCapturedMicSender = fixture.peers.some(peer =>
    peer.connectionState === 'connected' && peer.transceivers.some(slot =>
      ['sendonly', 'sendrecv'].includes(slot.currentDirection || slot.direction) &&
      slot.sender?.kind === 'audio' &&
      slot.sender.packetsProgressing &&
      ['microphone', 'microphone-clone', 'webaudio-microphone'].includes(slot.sender.lineage)));
  const hasProgressingReceiver = fixture.peers.some(peer =>
    peer.connectionState === 'connected' && peer.transceivers.some(slot =>
      ['recvonly', 'sendrecv'].includes(slot.currentDirection || slot.direction) &&
      slot.receiver?.kind === 'audio' &&
      slot.receiver.packetsProgressing));
  return hasCapturedMicSender && hasProgressingReceiver;
}

async function wave(directory, frequency) {
  const rate = 48000, frames = rate * 10, data = Buffer.alloc(44 + frames * 2);
  data.write('RIFF');
  data.writeUInt32LE(data.length - 8, 4);
  data.write('WAVEfmt ', 8);
  data.writeUInt32LE(16, 16);
  data.writeUInt16LE(1, 20);
  data.writeUInt16LE(1, 22);
  data.writeUInt32LE(rate, 24);
  data.writeUInt32LE(rate * 2, 28);
  data.writeUInt16LE(2, 32);
  data.writeUInt16LE(16, 34);
  data.write('data', 36);
  data.writeUInt32LE(frames * 2, 40);
  for (let i = 0; i < frames; i++) {
    data.writeInt16LE(Math.round(8000 * Math.sin((2 * Math.PI * frequency * i) / rate)), 44 + i * 2);
  }
  const file = path.join(directory, `mic-${frequency}.wav`);
  await writeFile(file, data);
  return file;
}

async function replayFixtureInPages(caller, remote, fixture, remoteToneGroups) {
  const offers = await caller.evaluate(async spec => {
    window.replayedGraphs = new Map();
    window.replayedPeers = [];
    window.replayedPlayers = [];
    let gumStream = null;
    let canvasVideoTrack = null;
    const ensureGum = async () => {
      if (!gumStream) {
        gumStream = await navigator.mediaDevices.getUserMedia({
          audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: false }
        });
      }
      return gumStream;
    };
    const ensureVideoTrack = () => {
      if (!canvasVideoTrack) {
        const canvas = document.createElement('canvas');
        canvas.width = 16;
        canvas.height = 16;
        canvas.getContext('2d').fillRect(0, 0, 16, 16);
        canvasVideoTrack = canvas.captureStream(5).getVideoTracks()[0];
      }
      return canvasVideoTrack;
    };

    for (const graphSpec of spec.webAudioGraphs) {
      const ctx = new AudioContext({ sampleRate: 48000 });
      const nodeObjects = new Map();
      const streamSources = new Map();
      let workletLoaded = false;
      const ensureWorklet = async () => {
        if (workletLoaded) return;
        const blob = new Blob([
          'registerProcessor("replay-passthrough", class extends AudioWorkletProcessor {' +
          '  process(inputs, outputs) {' +
          '    const input = inputs[0], output = outputs[0];' +
          '    if (input && output) {' +
          '      for (let c = 0; c < Math.min(input.length, output.length); c++) output[c].set(input[c]);' +
          '    }' +
          '    return true;' +
          '  }' +
          '});'
        ], { type: 'text/javascript' });
        const url = URL.createObjectURL(blob);
        try {
          await ctx.audioWorklet.addModule(url);
          workletLoaded = true;
        } finally {
          URL.revokeObjectURL(url);
        }
      };

      const nodeIndexById = new Map();
      graphSpec.nodes.forEach((nodeSpec, idx) => nodeIndexById.set(nodeSpec.id, idx));
      let nextNodeIdx = 0;

      const materializeNode = async nodeSpec => {
        if (nodeSpec.type === 'getUserMediaAudio') {
          streamSources.set(nodeSpec.id, await ensureGum());
        } else if (nodeSpec.type === 'getDisplayMediaAudio') {
          const canvas = document.createElement('canvas');
          canvas.width = 2;
          canvas.height = 2;
          streamSources.set(nodeSpec.id, canvas.captureStream(1));
        } else if (nodeSpec.type === 'MediaStreamAudioSourceNode') {
          const incomingStreamEdge = graphSpec.edges.find(e => e.to === nodeSpec.id && e.kind === 'stream');
          const stream = incomingStreamEdge ? streamSources.get(incomingStreamEdge.from) : await ensureGum();
          nodeObjects.set(nodeSpec.id, ctx.createMediaStreamSource(stream));
        } else if (nodeSpec.type === 'MediaStreamTrackAudioSourceNode') {
          const incomingStreamEdge = graphSpec.edges.find(e => e.to === nodeSpec.id && e.kind === 'stream');
          const stream = incomingStreamEdge ? streamSources.get(incomingStreamEdge.from) : await ensureGum();
          const track = stream.getAudioTracks()[0];
          nodeObjects.set(
            nodeSpec.id,
            typeof ctx.createMediaStreamTrackSource === 'function'
              ? ctx.createMediaStreamTrackSource(track)
              : ctx.createMediaStreamSource(new MediaStream([track]))
          );
        } else if (nodeSpec.type === 'MediaElementAudioSourceNode') {
          const el = new Audio();
          nodeObjects.set(nodeSpec.id, ctx.createMediaElementSource(el));
        } else if (nodeSpec.type === 'GainNode') {
          const gain = ctx.createGain();
          gain.gain.value = 1;
          nodeObjects.set(nodeSpec.id, gain);
        } else if (nodeSpec.type === 'BiquadFilterNode') {
          nodeObjects.set(nodeSpec.id, ctx.createBiquadFilter());
        } else if (nodeSpec.type === 'DynamicsCompressorNode') {
          nodeObjects.set(nodeSpec.id, ctx.createDynamicsCompressor());
        } else if (nodeSpec.type === 'AnalyserNode') {
          nodeObjects.set(nodeSpec.id, ctx.createAnalyser());
        } else if (nodeSpec.type === 'DelayNode') {
          nodeObjects.set(nodeSpec.id, ctx.createDelay());
        } else if (nodeSpec.type === 'ConvolverNode') {
          nodeObjects.set(nodeSpec.id, ctx.createConvolver());
        } else if (nodeSpec.type === 'WaveShaperNode') {
          nodeObjects.set(nodeSpec.id, ctx.createWaveShaper());
        } else if (nodeSpec.type === 'StereoPannerNode') {
          nodeObjects.set(nodeSpec.id, ctx.createStereoPanner());
        } else if (nodeSpec.type === 'PannerNode') {
          nodeObjects.set(nodeSpec.id, ctx.createPanner());
        } else if (nodeSpec.type === 'ChannelSplitterNode') {
          nodeObjects.set(nodeSpec.id, ctx.createChannelSplitter(nodeSpec.outputs ?? 6));
        } else if (nodeSpec.type === 'ChannelMergerNode') {
          nodeObjects.set(nodeSpec.id, ctx.createChannelMerger(nodeSpec.inputs ?? 6));
        } else if (nodeSpec.type === 'IIRFilterNode') {
          nodeObjects.set(nodeSpec.id, ctx.createIIRFilter([1], [1]));
        } else if (nodeSpec.type === 'ScriptProcessorNode') {
          nodeObjects.set(nodeSpec.id, ctx.createScriptProcessor(256, 1, 1));
        } else if (nodeSpec.type === 'AudioWorkletNode') {
          await ensureWorklet();
          nodeObjects.set(
            nodeSpec.id,
            new AudioWorkletNode(ctx, 'replay-passthrough', {
              numberOfInputs: nodeSpec.inputs ?? 1,
              numberOfOutputs: nodeSpec.outputs ?? 1
            })
          );
        } else if (nodeSpec.type === 'OscillatorNode') {
          const osc = ctx.createOscillator();
          osc.frequency.value = 440;
          osc.start();
          nodeObjects.set(nodeSpec.id, osc);
        } else if (nodeSpec.type === 'AudioBufferSourceNode') {
          const src = ctx.createBufferSource();
          const buf = ctx.createBuffer(1, 4800, 48000);
          src.buffer = buf;
          src.loop = true;
          src.start();
          nodeObjects.set(nodeSpec.id, src);
        } else if (nodeSpec.type === 'ConstantSourceNode') {
          const cs = ctx.createConstantSource();
          cs.offset.value = 0.01;
          cs.start();
          nodeObjects.set(nodeSpec.id, cs);
        } else if (nodeSpec.type === 'MediaStreamAudioDestinationNode') {
          nodeObjects.set(nodeSpec.id, ctx.createMediaStreamDestination());
        } else if (nodeSpec.type === 'AudioDestinationNode') {
          nodeObjects.set(nodeSpec.id, ctx.destination);
        } else {
          nodeObjects.set(nodeSpec.id, ctx.createGain());
        }
      };

      const materializeNodesThrough = async targetIdx => {
        while (nextNodeIdx <= targetIdx && nextNodeIdx < graphSpec.nodes.length) {
          await materializeNode(graphSpec.nodes[nextNodeIdx++]);
        }
      };

      const pickAudioParam = node => {
        for (const prop of [
          'gain', 'frequency', 'detune', 'Q', 'threshold', 'knee', 'ratio',
          'attack', 'release', 'pan', 'delayTime', 'offset', 'playbackRate'
        ]) {
          if (node[prop] instanceof AudioParam) return node[prop];
        }
        if (node.parameters && typeof node.parameters.values === 'function') {
          const first = node.parameters.values().next();
          if (!first.done && first.value instanceof AudioParam) return first.value;
        }
        return null;
      };

      // Materialize nodes just in time for each edge so that stream edges
      // (emitted on MediaStreamAudioSourceNode construction) and audio/param
      // edges (emitted on connect()) replay in the exact recorded order.
      for (const edge of graphSpec.edges) {
        const fromIdx = nodeIndexById.get(edge.from) ?? -1;
        const toIdx = nodeIndexById.get(edge.to) ?? -1;
        await materializeNodesThrough(Math.max(fromIdx, toIdx));
        if (edge.kind === 'stream') continue;
        const fromNode = nodeObjects.get(edge.from);
        const toNode = nodeObjects.get(edge.to);
        if (!fromNode || !toNode) continue;
        if (edge.kind === 'param') {
          const param = pickAudioParam(toNode);
          if (!param) throw new Error(`No AudioParam on target node ${edge.to}`);
          fromNode.connect(param, edge.output);
        } else {
          fromNode.connect(toNode, edge.output, edge.input);
        }
      }
      await materializeNodesThrough(graphSpec.nodes.length - 1);

      if (graphSpec.contextState === 'running') await ctx.resume();
      const outputTracks = new Map();
      for (const nodeSpec of graphSpec.nodes) {
        if (nodeSpec.type === 'MediaStreamAudioDestinationNode') {
          const destNode = nodeObjects.get(nodeSpec.id);
          const track = destNode?.stream.getAudioTracks()[0] || null;
          if (track) outputTracks.set(nodeSpec.id, track);
        }
      }
      window.replayedGraphs.set(graphSpec.id, { ctx, nodeObjects, outputTracks });
    }

    const peerOffers = [];
    for (const peerSpec of spec.peers) {
      const pc = new RTCPeerConnection();
      const usedTracksOnPc = new Set();
      for (let d = 0; d < peerSpec.dataChannelCount; d++) {
        pc.createDataChannel(`dc-${d}`);
      }
      for (const slotSpec of peerSpec.transceivers) {
        let senderTrack = null;
        if (slotSpec.sender) {
          if (slotSpec.sender.kind === 'video') {
            senderTrack = ensureVideoTrack();
          } else if (slotSpec.sender.webAudioGraphId) {
            const graph = window.replayedGraphs.get(slotSpec.sender.webAudioGraphId);
            senderTrack = graph?.outputTracks.get(slotSpec.sender.webAudioDestNodeId) || null;
            if (!senderTrack) {
              throw new Error(
                `Missing output track for ${slotSpec.sender.webAudioGraphId}.${slotSpec.sender.webAudioDestNodeId}`
              );
            }
          } else if (slotSpec.sender.lineage === 'microphone' || slotSpec.sender.lineage === 'microphone-clone') {
            senderTrack = (await ensureGum()).getAudioTracks()[0];
          }
          if (senderTrack && (slotSpec.sender.cloned || usedTracksOnPc.has(senderTrack))) {
            senderTrack = senderTrack.clone();
          }
          if (senderTrack) {
            usedTracksOnPc.add(senderTrack);
          }
        }
        if (senderTrack && slotSpec.direction === 'sendrecv') {
          pc.addTrack(senderTrack, new MediaStream([senderTrack]));
        } else if (senderTrack) {
          pc.addTransceiver(senderTrack, { direction: slotSpec.direction });
        } else {
          pc.addTransceiver(slotSpec.kind, { direction: slotSpec.direction });
        }
      }
      await pc.setLocalDescription(await pc.createOffer());
      if (pc.iceGatheringState !== 'complete') {
        await new Promise(resolve => {
          const check = () => {
            if (pc.iceGatheringState === 'complete') {
              pc.removeEventListener('icegatheringstatechange', check);
              resolve();
            }
          };
          pc.addEventListener('icegatheringstatechange', check);
          check();
        });
      }
      window.replayedPeers.push(pc);
      peerOffers.push({ peer: peerSpec.peer, offer: pc.localDescription.toJSON(), slots: peerSpec.transceivers });
    }
    return peerOffers;
  }, fixture);

  const answers = await remote.evaluate(async ({ offers, remoteToneGroups }) => {
    window.remotePeers = [];
    window.remoteAudio = new AudioContext({ sampleRate: 48000 });
    window.remoteRetained = [];
    let groupCursor = 0;

    function buildToneTrack(frequencies) {
      const output = window.remoteAudio.createMediaStreamDestination();
      for (const freq of frequencies) {
        const osc = window.remoteAudio.createOscillator();
        const gain = window.remoteAudio.createGain();
        osc.frequency.value = freq;
        gain.gain.value = 0.25;
        osc.connect(gain);
        gain.connect(output);
        osc.start();
        window.remoteRetained.push(osc, gain);
      }
      window.remoteRetained.push(output);
      return output.stream.getAudioTracks()[0];
    }

    function buildVideoTrack() {
      const canvas = document.createElement('canvas');
      canvas.width = 16;
      canvas.height = 16;
      canvas.getContext('2d').fillRect(0, 0, 16, 16);
      return canvas.captureStream(5).getVideoTracks()[0];
    }

    const out = [];
    for (const item of offers) {
      const pc = new RTCPeerConnection();
      await pc.setRemoteDescription(item.offer);
      const transceivers = pc.getTransceivers();
      for (let i = 0; i < item.slots.length; i++) {
        const slotSpec = item.slots[i];
        const transceiver = transceivers[i];
        if (!transceiver) continue;
        if (slotSpec.receiver && ['sendrecv', 'recvonly'].includes(slotSpec.currentDirection || slotSpec.direction)) {
          if (slotSpec.receiver.kind === 'video') {
            await transceiver.sender.replaceTrack(buildVideoTrack());
          } else {
            const tones = remoteToneGroups[groupCursor++] || [550];
            await transceiver.sender.replaceTrack(buildToneTrack(tones));
          }
          transceiver.direction = ['sendrecv', 'sendonly'].includes(slotSpec.direction) ? 'sendrecv' : 'sendonly';
        }
      }
      await window.remoteAudio.resume();
      await pc.setLocalDescription(await pc.createAnswer());
      if (pc.iceGatheringState !== 'complete') {
        await new Promise(resolve => {
          const check = () => {
            if (pc.iceGatheringState === 'complete') {
              pc.removeEventListener('icegatheringstatechange', check);
              resolve();
            }
          };
          pc.addEventListener('icegatheringstatechange', check);
          check();
        });
      }
      window.remotePeers.push(pc);
      out.push({ peer: item.peer, answer: pc.localDescription.toJSON() });
    }
    return out;
  }, { offers, remoteToneGroups });

  await caller.evaluate(async answers => {
    for (const item of answers) {
      const pc = window.replayedPeers[item.peer];
      await pc.setRemoteDescription(item.answer);
    }
  }, answers);

  await caller.waitForFunction(() =>
    window.replayedPeers.length > 0 && window.replayedPeers.every(pc => pc.connectionState === 'connected'));

  await caller.evaluate(async () => {
    for (const pc of window.replayedPeers) {
      for (const slot of pc.getTransceivers()) {
        if (!['recvonly', 'sendrecv'].includes(slot.currentDirection) || slot.receiver.track?.kind !== 'audio') continue;
        const player = new Audio();
        player.srcObject = new MediaStream([slot.receiver.track]);
        (document.body || document.documentElement).append(player);
        await player.play();
        window.replayedPlayers.push(player);
      }
    }
  });
}

const temporary = await mkdtemp(path.join(os.tmpdir(), 'cassini-platform-replay-'));
const extensionDir = path.join(temporary, 'extension');
const profileDir = path.join(temporary, 'profile');
const audioRoot = path.join(temporary, 'recordings');
const hostsDir = path.join(profileDir, 'NativeMessagingHosts');

await cp(path.resolve(process.env.CASSINI_TEST_EXTENSION || path.join(root, 'extension')), extensionDir, { recursive: true });
await Promise.all([mkdir(audioRoot, { recursive: true }), mkdir(hostsDir, { recursive: true }), mkdir(path.join(root, 'artifacts'), { recursive: true })]);

const manifestPath = path.join(extensionDir, 'manifest.json');
const manifest = JSON.parse(await readFile(manifestPath, 'utf8'));
const key = generateKeyPairSync('rsa', { modulusLength: 2048 }).publicKey.export({ type: 'spki', format: 'der' });
manifest.key = key.toString('base64');
await writeFile(manifestPath, JSON.stringify(manifest));
const extensionId = [...createHash('sha256').update(key).digest('hex').slice(0, 32)]
  .map(c => String.fromCharCode(97 + parseInt(c, 16))).join('');
assert.notEqual(extensionId, 'pcafdaahfhjiolfpfnlhmljfgoabbaij');

const controlSocketPath = path.join(temporary, 'control.sock');
let controlPeer = null;
const controlEvents = [];
const controlListeners = new Set();

const controlServer = createNetServer(peer => {
  controlPeer = peer;
  let pending = '';
  peer.setEncoding('utf8');
  peer.on('data', chunk => {
    pending += chunk;
    for (let end; (end = pending.indexOf('\n')) >= 0;) {
      controlEvents.push(JSON.parse(pending.slice(0, end)));
      pending = pending.slice(end + 1);
      for (const listener of [...controlListeners]) listener();
    }
  });
});
await new Promise(resolve => controlServer.listen(controlSocketPath, resolve));

for (const [name, script] of [
  ['io.codemyriad.cassini', 'browser_host_fixture.py'],
  ['io.codemyriad.cassini.control', 'control_host_fixture.py']
]) {
  const wrapper = path.join(temporary, `${name}.py`);
  await writeFile(
    wrapper,
    `#!/usr/bin/env python3\nimport os, sys\n` +
    `os.environ['CASSINI_LIBRARY_DIR'] = ${JSON.stringify(audioRoot)}\n` +
    `os.environ['CASSINI_CONTROL_SOCKET'] = ${JSON.stringify(controlSocketPath)}\n` +
    `sys.argv = [${JSON.stringify(path.join(root, 'tests', script))}, ${JSON.stringify(audioRoot)}]\n` +
    `exec(compile(open(sys.argv[0]).read(), sys.argv[0], 'exec'))\n`
  );
  await chmod(wrapper, 0o700);
  await writeFile(
    path.join(hostsDir, `${name}.json`),
    JSON.stringify({
      name,
      description: 'Private platform-topology replay host',
      path: wrapper,
      type: 'stdio',
      allowed_origins: [`chrome-extension://${extensionId}/`]
    })
  );
}

function waitForControlEvent(predicate, label, timeout = 20000) {
  return eventValue(changed => {
    controlListeners.add(changed);
    return () => controlListeners.delete(changed);
  }, predicate, { label, timeout });
}

function sessionValue(directory, read, label, timeout = 20000) {
  return eventValue((changed, failed) => {
    const watcher = watch(directory, changed);
    watcher.on('error', failed);
    return () => watcher.close();
  }, read, { label, timeout });
}

const httpServer = createHttpServer((request, response) => {
  response.setHeader('Content-Type', 'text/html; charset=utf-8');
  response.end(`<!doctype html><title>Synthetic platform replay</title><main id="app">${request.url}</main>`);
});
await new Promise(resolve => httpServer.listen(0, '127.0.0.1', resolve));
const baseUrl = `http://127.0.0.1:${httpServer.address().port}`;

const report = {
  schema: 'cassini-platform-topology-replay-report/v1',
  extensionVersion: manifest.version,
  replayedFixtures: [],
  regressionControls: [],
  negativeControl: null
};

let context;
const pageErrors = [];
try {
  const micWav = await wave(temporary, 330);
  context = await chromium.launchPersistentContext(profileDir, {
    channel: 'chromium',
    headless: true,
    permissions: ['microphone'],
    args: [
      '--autoplay-policy=no-user-gesture-required',
      '--disable-audio-output',
      '--use-fake-device-for-media-stream',
      '--use-fake-ui-for-media-stream',
      `--use-file-for-fake-audio-capture=${micWav}`,
      '--allow-loopback-in-peer-connection',
      '--disable-features=WebRtcHideLocalIpsWithMdns',
      `--disable-extensions-except=${extensionDir}`,
      `--load-extension=${extensionDir}`,
      `--allowlisted-extension-id=${extensionId}`
    ]
  });
  await context.addInitScript(installRtcTopologyProbe);
  const worker = context.serviceWorkers()[0] || await context.waitForEvent('serviceworker');

  await waitForControlEvent(() => controlEvents.find(e => e.type === 'hello'), 'control hello');
  const configStart = controlEvents.length;
  controlPeer.write(JSON.stringify({
    type: 'configure',
    capabilities: ['call-signals-v1', 'verified-auto-admission-v1']
  }) + '\n');
  await waitForControlEvent(
    () => controlEvents.slice(configStart).find(e => e.type === 'configured'),
    'control configured'
  );

  for (const { file, fixture } of fixtures) {
    const callerUrl = `${baseUrl}/replay/${fixture.platform}/caller`;
    const remoteUrl = `${baseUrl}/replay/${fixture.platform}/remote`;
    const caller = await context.newPage();
    const remote = await context.newPage();
    caller.on('pageerror', error => pageErrors.push(error.message));
    remote.on('pageerror', error => pageErrors.push(error.message));

    try {
      await Promise.all([caller.goto(callerUrl), remote.goto(remoteUrl)]);
      const REMOTE_FREQUENCIES = [550, 880, 1230, 1480];
      let freqCursor = 0;
      const activeAudioReceivers = fixture.peers.flatMap(peer =>
        peer.transceivers.filter(slot =>
          slot.kind === 'audio' &&
          slot.receiver?.kind === 'audio' &&
          ['sendrecv', 'recvonly'].includes(slot.currentDirection || slot.direction))
      );
      const remoteToneGroups = activeAudioReceivers.map(slot => {
        const count = (slot.receiver.mixing === 'mixed' || (slot.receiver.speakersOnTrack ?? 0) >= 2)
          ? Math.min(Math.max(slot.receiver.speakersOnTrack || 2, 2), 3)
          : 1;
        const tones = [];
        for (let k = 0; k < count; k++) {
          tones.push(REMOTE_FREQUENCIES[freqCursor++ % REMOTE_FREQUENCIES.length]);
        }
        return tones;
      });
      const allRemoteFrequencies = [...new Set(remoteToneGroups.flat())];

      await replayFixtureInPages(caller, remote, fixture, remoteToneGroups);

      const shouldAdmit = expectedAutoAdmission(fixture);
      const sendingMode = shouldAdmit ? 'captured' : 'any';
      const before = await waitRtcTopology(caller, { sending: sendingMode });
      const topology = summarizeRtcTopology(before, await waitRtcTopology(caller, { previous: before, sending: sendingMode }));

      const firstRx = activeAudioReceivers[0]?.receiver || null;
      const dumpedPath = path.join(temporary, `dumped-${file}`);
      const dumped = await dumpPlatformTopologyFixture(caller, {
        outPath: dumpedPath,
        platform: fixture.platform,
        capturedOn: fixture.capturedOn,
        provenance: 'synthetic-probe',
        localSpeakers: fixture.call.localSpeakers,
        remoteSpeakers: fixture.call.remoteSpeakers,
        ...(firstRx ? { receiverMixing: firstRx.mixing } : {})
      });
      assert.equal(dumped.peers.length, fixture.peers.length, `${file}: peer count round-trip`);
      assert.equal(dumped.webAudioGraphs.length, fixture.webAudioGraphs.length, `${file}: WebAudio graph count round-trip`);
      fixture.webAudioGraphs.forEach((expectedGraph, gIndex) => {
        assert.deepEqual(dumped.webAudioGraphs[gIndex].nodes, expectedGraph.nodes, `${file}: graph ${gIndex} nodes`);
        assert.deepEqual(dumped.webAudioGraphs[gIndex].edges, expectedGraph.edges, `${file}: graph ${gIndex} edges`);
        assert.equal(dumped.webAudioGraphs[gIndex].outputTrackOrigin, expectedGraph.outputTrackOrigin);
      });
      fixture.peers.forEach((expectedPeer, pIndex) => {
        const actualPeer = dumped.peers[pIndex];
        assert.equal(actualPeer.connectionState, expectedPeer.connectionState, `${file}: peer ${pIndex} state`);
        assert.equal(actualPeer.dataChannelCount, expectedPeer.dataChannelCount, `${file}: peer ${pIndex} dataChannels`);
        assert.equal(actualPeer.transceivers.length, expectedPeer.transceivers.length, `${file}: peer ${pIndex} slots`);
        expectedPeer.transceivers.forEach((expectedSlot, sIndex) => {
          const actualSlot = actualPeer.transceivers[sIndex];
          assert.equal(actualSlot.kind, expectedSlot.kind, `${file}: peer ${pIndex} slot ${sIndex} kind`);
          assert.equal(actualSlot.currentDirection, expectedSlot.currentDirection);
          assert.equal(actualSlot.sender?.label ?? null, expectedSlot.sender?.label ?? null);
          assert.equal(actualSlot.sender?.lineage ?? null, expectedSlot.sender?.lineage ?? null);
          assert.equal(actualSlot.sender?.webAudioGraphId ?? null, expectedSlot.sender?.webAudioGraphId ?? null);
          assert.equal(actualSlot.sender?.webAudioDestNodeId ?? null, expectedSlot.sender?.webAudioDestNodeId ?? null);
          assert.equal(actualSlot.sender?.cloned ?? null, expectedSlot.sender?.cloned ?? null);
          assert.equal(actualSlot.receiver?.label ?? null, expectedSlot.receiver?.label ?? null);
          assert.equal(actualSlot.receiver?.mixing ?? null, expectedSlot.receiver?.mixing ?? null);
        });
      });

      const tabId = await worker.evaluate(
        async targetUrl => (await chrome.tabs.query({})).find(tab => tab.url === targetUrl).id,
        callerUrl
      );

      const signalBarrier = controlEvents.length;
      controlPeer.write(JSON.stringify({
        type: 'configure',
        capabilities: ['call-signals-v1', 'verified-auto-admission-v1']
      }) + '\n');
      const expectedState = shouldAdmit ? 'active' : 'unknown';
      const published = await waitForControlEvent(
        () => controlEvents
          .slice(signalBarrier)
          .filter(e => e.type === 'callSignals')
          .flatMap(e => e.tabs)
          .find(tab => tab.tabId === tabId && tab.callEvidence.state === expectedState),
        `${file}: worker callEvidence=${expectedState}`
      );
      const collectorSnapshot = await caller.evaluate(() => {
        const v = globalThis.__cassiniCallEvidenceV1.read();
        return {
          cohortQualified: v.cohortQualified,
          capturedSendingPeers: v.capturedSendingPeers,
          receivedPeers: v.receivedPeers,
          samePeerCount: v.samePeerCount
        };
      });
      assert.equal(collectorSnapshot.cohortQualified, shouldAdmit, `${file}: native collector cohortQualified`);
      let recordedTracksSummary = [];
      if (shouldAdmit) {
        const verifiedMeeting = await waitForControlEvent(
          () => (controlEvents.filter(e => e.type === 'meetings').at(-1)?.meetings || [])
            .find(m => m.tabId === tabId && m.browserDocumentId === published.documentGeneration),
          `${file}: correlated verified meeting inventory`
        );
        assert.equal(verifiedMeeting.callGeneration, published.callEvidence.callGeneration);

        // Exercise real separate-track capture on the replayed fixture: local
        // sender ("Me") + remote receiver track(s) must record cleanly without cross-bleed.
        const startId = `replay-start-${fixture.platform}`;
        controlPeer.write(JSON.stringify({
          type: 'command',
          requestId: startId,
          action: 'start',
          tabId,
          mode: 'tracks',
          independent: true,
          captureLocal: true,
          microphone: false
        }) + '\n');
        const started = await waitForControlEvent(
          () => controlEvents.find(e => e.type === 'result' && e.requestId === startId),
          `${file}: native start result`
        );
        assert.equal(started.ok, true, JSON.stringify(started));
        const recordingDir = started.status.path;

        await sessionValue(recordingDir, async () => {
          try {
            const session = JSON.parse(await readFile(path.join(recordingDir, 'session.json'), 'utf8'));
            const local = session.tracks.find(t => t.kind === 'local');
            const remotes = session.tracks.filter(t => t.kind === 'remote');
            if (!local || remotes.length < activeAudioReceivers.length) return false;
            if (local.frames < local.sampleRate * 2 || remotes.some(r => r.frames < r.sampleRate * 2)) return false;
            const localWav = await readFile(path.join(recordingDir, local.file));
            const remoteWavs = await Promise.all(remotes.map(r => readFile(path.join(recordingDir, r.file))));
            const localFirst = Math.max(4800, local.frames - 14000);
            if (tonePower(localWav, 48000, 330, localFirst, 12000) <= 0.0005) return false;
            return allRemoteFrequencies.every(freq =>
              remoteWavs.some((wav, idx) => {
                const offset = Math.max(4800, remotes[idx].frames - 14000);
                return tonePower(wav, 48000, freq, offset, 12000) > 0.001;
              })
            );
          } catch {
            return false;
          }
        }, `${file}: settled local 330 Hz + remote WAVs`);

        const stopId = `replay-stop-${fixture.platform}`;
        controlPeer.write(JSON.stringify({
          type: 'command',
          requestId: stopId,
          action: 'stop',
          tabId,
          independent: true
        }) + '\n');
        const stopped = await waitForControlEvent(
          () => controlEvents.find(e => e.type === 'result' && e.requestId === stopId),
          `${file}: native stop result`
        );
        assert.equal(stopped.ok, true, JSON.stringify(stopped));

        const finalManifest = JSON.parse(await readFile(path.join(recordingDir, 'session.json'), 'utf8'));
        assert.equal(finalManifest.state, 'completed');
        const localTracks = finalManifest.tracks.filter(t => t.kind === 'local');
        const remoteTracks = finalManifest.tracks.filter(t => t.kind === 'remote');
        assert.equal(localTracks.length, 1, `${file}: one local Me track captured`);
        assert.equal(remoteTracks.length, activeAudioReceivers.length, `${file}: expected remote receiver track count`);
        if (fixture.platform === 'ms-teams') {
          assert.equal(localTracks[0].label, 'local: MediaStreamAudioDestinationNode');
          assert.equal(remoteTracks.length, 1, 'MS Teams mixes remote speakers onto one receiver track');
        } else {
          assert.match(localTracks[0].label, /^local: /);
        }
        remoteTracks.forEach(rt => assert.match(rt.label, /^remote: /));

        const localWav = await readFile(path.join(recordingDir, localTracks[0].file));
        const localOffset = Math.max(4800, localTracks[0].frames - 14000);
        const local330 = tonePower(localWav, 48000, 330, localOffset, 12000);
        assert.ok(local330 > 0.0005, `${file}: local Me track carries 330 Hz mic signal`);
        for (const freq of allRemoteFrequencies) {
          assert.ok(tonePower(localWav, 48000, freq, localOffset, 12000) < local330 * 0.01, `${file}: no ${freq} Hz remote bleed into local track`);
        }

        const remoteWavs = await Promise.all(remoteTracks.map(rt => readFile(path.join(recordingDir, rt.file))));
        for (const freq of allRemoteFrequencies) {
          const maxPower = Math.max(...remoteWavs.map((wav, idx) =>
            tonePower(wav, 48000, freq, Math.max(4800, remoteTracks[idx].frames - 14000), 12000)));
          assert.ok(maxPower > 0.001, `${file}: remote voice ${freq} Hz preserved on captured remote WAV`);
        }
        remoteWavs.forEach((wav, idx) => {
          const offset = Math.max(4800, remoteTracks[idx].frames - 14000);
          const remote330 = tonePower(wav, 48000, 330, offset, 12000);
          const strongestRemote = Math.max(...allRemoteFrequencies.map(freq => tonePower(wav, 48000, freq, offset, 12000)));
          assert.ok(remote330 < strongestRemote * 0.01, `${file}: no local 330 Hz mic bleed into remote track ${idx}`);
        });

        recordedTracksSummary = finalManifest.tracks.map(t => ({
          kind: t.kind,
          hasExpectedPrefix: t.label.startsWith(`${t.kind}: `),
          frames: t.frames,
          captureOutcome: t.captureOutcome || 'captured'
        }));
      }

      report.replayedFixtures.push({
        file,
        platform: fixture.platform,
        topology: {
          sendingPeers: topology.sendingPeers,
          receivingPeers: topology.receivingPeers,
          samePeerConnection: topology.samePeerConnection
        },
        collector: collectorSnapshot,
        autoAdmissionState: published.callEvidence.state,
        recordedTracks: recordedTracksSummary
      });
      console.log(`PASS platform topology replay: ${file} (auto-admission=${published.callEvidence.state}, tracks=${recordedTracksSummary.length})`);
    } finally {
      await caller.close();
      await remote.close();
    }
  }

  // Regression controls for multi-destination contexts and port-sensitive routing:
  // 1a) Two MediaStreamAudioDestinationNodes in one AudioContext sent on two senders
  //     of the same RTCPeerConnection must replay without InvalidAccessError and
  //     preserve each sender's destination node ID and lineage.
  // 1b) Single sender selecting the SECOND destination node in one AudioContext
  //     must receive that second output's 330 Hz mic audio (never the first
  //     destination's 440 Hz oscillator).
  // 2a) Real ChannelSplitterNode -> (0, 0) -> real ChannelMergerNode -> (0, 0)
  //     preserves port-0 microphone lineage and qualifies for auto-admission.
  // 2b) source.connect(merger, 0, 1) into real ChannelMergerNode input 1 dumps
  //     distinct port topology and withholds microphone lineage (state=unknown).
  {
    const twoDestGraph = {
      id: 'graph-0',
      contextState: 'running',
      nodes: [
        { id: 'n0', type: 'OscillatorNode', origin: 'synthetic' },
        { id: 'n1', type: 'MediaStreamAudioDestinationNode' },
        { id: 'n2', type: 'getUserMediaAudio', origin: 'microphone' },
        { id: 'n3', type: 'MediaStreamAudioSourceNode' },
        { id: 'n4', type: 'GainNode' },
        { id: 'n5', type: 'MediaStreamAudioDestinationNode' }
      ],
      edges: [
        { from: 'n0', to: 'n1', kind: 'audio', output: 0, input: 0 },
        { from: 'n2', to: 'n3', kind: 'stream' },
        { from: 'n3', to: 'n4', kind: 'audio', output: 0, input: 0 },
        { from: 'n4', to: 'n5', kind: 'audio', output: 0, input: 0 }
      ],
      outputTrackOrigin: 'microphone'
    };

    const regressionCases = [
      {
        name: 'two-dest-two-senders',
        verifySecondDestAudio: false,
        fixture: validatePlatformTopologyFixture({
          schema: 'cassini-platform-topology/v1',
          platform: 'two-dest-two-senders',
          capturedOn: '2026-11-12',
          provenance: 'synthetic-probe',
          call: { localSpeakers: 1, remoteSpeakers: 1 },
          runtime: { usesWebAudio: true, usesAudioWorklet: false, usesWasm: false },
          webAudioGraphs: [structuredClone(twoDestGraph)],
          peers: [
            {
              peer: 0,
              connectionState: 'connected',
              dataChannelCount: 0,
              transceivers: [
                {
                  slot: 0,
                  kind: 'audio',
                  direction: 'sendonly',
                  currentDirection: 'sendonly',
                  sender: {
                    kind: 'audio',
                    label: 'MediaStreamAudioDestinationNode',
                    lineage: 'webaudio-synthetic',
                    webAudioGraphId: 'graph-0',
                    webAudioDestNodeId: 'n1',
                    cloned: false,
                    packetsProgressing: true
                  },
                  receiver: null
                },
                {
                  slot: 1,
                  kind: 'audio',
                  direction: 'sendrecv',
                  currentDirection: 'sendrecv',
                  sender: {
                    kind: 'audio',
                    label: 'MediaStreamAudioDestinationNode',
                    lineage: 'webaudio-microphone',
                    webAudioGraphId: 'graph-0',
                    webAudioDestNodeId: 'n5',
                    cloned: false,
                    packetsProgressing: true
                  },
                  receiver: {
                    kind: 'audio',
                    label: 'remote audio',
                    mixing: 'single',
                    speakersOnTrack: 1,
                    speakersOverTime: [1],
                    csrc: { present: false, distinctCount: 0 },
                    ssrc: { present: true, distinctCount: 1 },
                    packetsProgressing: true
                  }
                }
              ]
            }
          ]
        })
      },
      {
        name: 'two-dest-second-only',
        verifySecondDestAudio: true,
        fixture: validatePlatformTopologyFixture({
          schema: 'cassini-platform-topology/v1',
          platform: 'two-dest-second-only',
          capturedOn: '2026-11-12',
          provenance: 'synthetic-probe',
          call: { localSpeakers: 1, remoteSpeakers: 1 },
          runtime: { usesWebAudio: true, usesAudioWorklet: false, usesWasm: false },
          webAudioGraphs: [structuredClone(twoDestGraph)],
          peers: [
            {
              peer: 0,
              connectionState: 'connected',
              dataChannelCount: 0,
              transceivers: [
                {
                  slot: 0,
                  kind: 'audio',
                  direction: 'sendrecv',
                  currentDirection: 'sendrecv',
                  sender: {
                    kind: 'audio',
                    label: 'MediaStreamAudioDestinationNode',
                    lineage: 'webaudio-microphone',
                    webAudioGraphId: 'graph-0',
                    webAudioDestNodeId: 'n5',
                    cloned: false,
                    packetsProgressing: true
                  },
                  receiver: {
                    kind: 'audio',
                    label: 'remote audio',
                    mixing: 'single',
                    speakersOnTrack: 1,
                    speakersOverTime: [1],
                    csrc: { present: false, distinctCount: 0 },
                    ssrc: { present: true, distinctCount: 1 },
                    packetsProgressing: true
                  }
                }
              ]
            }
          ]
        })
      },
      {
        name: 'port-zero-splitter-merger',
        verifySecondDestAudio: false,
        fixture: validatePlatformTopologyFixture({
          schema: 'cassini-platform-topology/v1',
          platform: 'port-zero-splitter-merger',
          capturedOn: '2026-11-12',
          provenance: 'synthetic-probe',
          call: { localSpeakers: 1, remoteSpeakers: 1 },
          runtime: { usesWebAudio: true, usesAudioWorklet: false, usesWasm: false },
          webAudioGraphs: [
            {
              id: 'graph-0',
              contextState: 'running',
              nodes: [
                { id: 'n0', type: 'getUserMediaAudio', origin: 'microphone' },
                { id: 'n1', type: 'MediaStreamAudioSourceNode' },
                { id: 'n2', type: 'ChannelSplitterNode', outputs: 2 },
                { id: 'n3', type: 'ChannelMergerNode', inputs: 2 },
                { id: 'n4', type: 'MediaStreamAudioDestinationNode' }
              ],
              edges: [
                { from: 'n0', to: 'n1', kind: 'stream' },
                { from: 'n1', to: 'n2', kind: 'audio', output: 0, input: 0 },
                { from: 'n2', to: 'n3', kind: 'audio', output: 0, input: 0 },
                { from: 'n3', to: 'n4', kind: 'audio', output: 0, input: 0 }
              ],
              outputTrackOrigin: 'microphone'
            }
          ],
          peers: [
            {
              peer: 0,
              connectionState: 'connected',
              dataChannelCount: 0,
              transceivers: [
                {
                  slot: 0,
                  kind: 'audio',
                  direction: 'sendrecv',
                  currentDirection: 'sendrecv',
                  sender: {
                    kind: 'audio',
                    label: 'MediaStreamAudioDestinationNode',
                    lineage: 'webaudio-microphone',
                    webAudioGraphId: 'graph-0',
                    webAudioDestNodeId: 'n4',
                    cloned: false,
                    packetsProgressing: true
                  },
                  receiver: {
                    kind: 'audio',
                    label: 'remote audio',
                    mixing: 'single',
                    speakersOnTrack: 1,
                    speakersOverTime: [1],
                    csrc: { present: false, distinctCount: 0 },
                    ssrc: { present: true, distinctCount: 1 },
                    packetsProgressing: true
                  }
                }
              ]
            }
          ]
        })
      },
      {
        name: 'port-nonzero-merger',
        verifySecondDestAudio: false,
        fixture: validatePlatformTopologyFixture({
          schema: 'cassini-platform-topology/v1',
          platform: 'port-nonzero-merger',
          capturedOn: '2026-11-12',
          provenance: 'synthetic-probe',
          call: { localSpeakers: 0, remoteSpeakers: 1 },
          runtime: { usesWebAudio: true, usesAudioWorklet: false, usesWasm: false },
          webAudioGraphs: [
            {
              id: 'graph-0',
              contextState: 'running',
              nodes: [
                { id: 'n0', type: 'getUserMediaAudio', origin: 'microphone' },
                { id: 'n1', type: 'MediaStreamAudioSourceNode' },
                { id: 'n2', type: 'ChannelMergerNode', inputs: 2 },
                { id: 'n3', type: 'MediaStreamAudioDestinationNode' }
              ],
              edges: [
                { from: 'n0', to: 'n1', kind: 'stream' },
                { from: 'n1', to: 'n2', kind: 'audio', output: 0, input: 1 },
                { from: 'n2', to: 'n3', kind: 'audio', output: 0, input: 0 }
              ],
              outputTrackOrigin: 'none'
            }
          ],
          peers: [
            {
              peer: 0,
              connectionState: 'connected',
              dataChannelCount: 0,
              transceivers: [
                {
                  slot: 0,
                  kind: 'audio',
                  direction: 'sendrecv',
                  currentDirection: 'sendrecv',
                  sender: {
                    kind: 'audio',
                    label: 'MediaStreamAudioDestinationNode',
                    lineage: 'unknown',
                    webAudioGraphId: 'graph-0',
                    webAudioDestNodeId: 'n3',
                    cloned: false,
                    packetsProgressing: true
                  },
                  receiver: {
                    kind: 'audio',
                    label: 'remote audio',
                    mixing: 'single',
                    speakersOnTrack: 1,
                    speakersOverTime: [1],
                    csrc: { present: false, distinctCount: 0 },
                    ssrc: { present: true, distinctCount: 1 },
                    packetsProgressing: true
                  }
                }
              ]
            }
          ]
        })
      }
    ];

    for (const { name, verifySecondDestAudio, fixture } of regressionCases) {
      const regCallerUrl = `${baseUrl}/replay/${name}/caller`;
      const regRemoteUrl = `${baseUrl}/replay/${name}/remote`;
      const regCaller = await context.newPage();
      const regRemote = await context.newPage();
      regCaller.on('pageerror', error => pageErrors.push(error.message));
      regRemote.on('pageerror', error => pageErrors.push(error.message));

      try {
        await Promise.all([regCaller.goto(regCallerUrl), regRemote.goto(regRemoteUrl)]);
        await replayFixtureInPages(regCaller, regRemote, fixture, [[550]]);

        const shouldAdmit = expectedAutoAdmission(fixture);
        const sendingMode = shouldAdmit ? 'captured' : 'any';
        const before = await waitRtcTopology(regCaller, { sending: sendingMode });
        const topology = summarizeRtcTopology(
          before,
          await waitRtcTopology(regCaller, { previous: before, sending: sendingMode })
        );
        assert.equal(topology.sendingPeers, shouldAdmit ? 1 : 0, `${name}: sendingPeers`);
        assert.equal(topology.receivingPeers, 1, `${name}: receivingPeers`);

        const dumped = await dumpPlatformTopologyFixture(regCaller, {
          platform: fixture.platform,
          capturedOn: fixture.capturedOn,
          provenance: 'synthetic-probe',
          localSpeakers: fixture.call.localSpeakers,
          remoteSpeakers: fixture.call.remoteSpeakers,
          receiverMixing: 'single'
        });
        assert.deepEqual(dumped.webAudioGraphs[0].nodes, fixture.webAudioGraphs[0].nodes, `${name}: nodes round-trip`);
        assert.deepEqual(dumped.webAudioGraphs[0].edges, fixture.webAudioGraphs[0].edges, `${name}: edges round-trip`);
        assert.equal(dumped.webAudioGraphs[0].outputTrackOrigin, fixture.webAudioGraphs[0].outputTrackOrigin, `${name}: outputTrackOrigin`);
        fixture.peers[0].transceivers.forEach((expectedSlot, sIndex) => {
          const actualSlot = dumped.peers[0].transceivers[sIndex];
          assert.equal(actualSlot.sender?.lineage ?? null, expectedSlot.sender?.lineage ?? null, `${name}: slot ${sIndex} lineage`);
          assert.equal(actualSlot.sender?.webAudioGraphId ?? null, expectedSlot.sender?.webAudioGraphId ?? null, `${name}: slot ${sIndex} graphId`);
          assert.equal(actualSlot.sender?.webAudioDestNodeId ?? null, expectedSlot.sender?.webAudioDestNodeId ?? null, `${name}: slot ${sIndex} destNodeId`);
        });

        await regCaller.waitForFunction(() => globalThis.__cassiniCallEvidenceV1?.read().revision >= 3);
        const regTabId = await worker.evaluate(
          async targetUrl => (await chrome.tabs.query({})).find(tab => tab.url === targetUrl).id,
          regCallerUrl
        );
        const regBarrier = controlEvents.length;
        controlPeer.write(JSON.stringify({
          type: 'configure',
          capabilities: ['call-signals-v1', 'verified-auto-admission-v1']
        }) + '\n');
        const expectedState = shouldAdmit ? 'active' : 'unknown';
        const regRow = await waitForControlEvent(
          () => controlEvents
            .slice(regBarrier)
            .filter(e => e.type === 'callSignals')
            .flatMap(e => e.tabs)
            .find(tab => tab.tabId === regTabId && (!shouldAdmit || tab.callEvidence.state === expectedState)),
          `${name}: callSignals snapshot (${expectedState})`
        );
        const regCollector = await regCaller.evaluate(() => {
          const v = globalThis.__cassiniCallEvidenceV1.read();
          return {
            cohortQualified: v.cohortQualified,
            capturedSendingPeers: v.capturedSendingPeers,
            receivedPeers: v.receivedPeers
          };
        });
        assert.equal(regCollector.cohortQualified, shouldAdmit, `${name}: cohortQualified`);
        assert.equal(regCollector.capturedSendingPeers, shouldAdmit ? 1 : 0, `${name}: capturedSendingPeers`);
        assert.equal(regRow.callEvidence.state, expectedState, `${name}: callEvidence.state`);

        if (verifySecondDestAudio) {
          const startId = `replay-start-${name}`;
          controlPeer.write(JSON.stringify({
            type: 'command',
            requestId: startId,
            action: 'start',
            tabId: regTabId,
            mode: 'tracks',
            independent: true,
            captureLocal: true,
            microphone: false
          }) + '\n');
          const started = await waitForControlEvent(
            () => controlEvents.find(e => e.type === 'result' && e.requestId === startId),
            `${name}: start result`
          );
          assert.equal(started.ok, true, JSON.stringify(started));
          const recordingDir = started.status.path;
          await sessionValue(recordingDir, async () => {
            try {
              const session = JSON.parse(await readFile(path.join(recordingDir, 'session.json'), 'utf8'));
              const local = session.tracks.find(t => t.kind === 'local');
              if (!local || local.frames < local.sampleRate * 2) return false;
              const wav = await readFile(path.join(recordingDir, local.file));
              const offset = Math.max(4800, local.frames - 14000);
              return tonePower(wav, 48000, 330, offset, 12000) > 0.0005;
            } catch {
              return false;
            }
          }, `${name}: settled 330 Hz second-destination local WAV`);
          const stopId = `replay-stop-${name}`;
          controlPeer.write(JSON.stringify({
            type: 'command',
            requestId: stopId,
            action: 'stop',
            tabId: regTabId,
            independent: true
          }) + '\n');
          const stopped = await waitForControlEvent(
            () => controlEvents.find(e => e.type === 'result' && e.requestId === stopId),
            `${name}: stop result`
          );
          assert.equal(stopped.ok, true, JSON.stringify(stopped));
          const finalSession = JSON.parse(await readFile(path.join(recordingDir, 'session.json'), 'utf8'));
          const localTrack = finalSession.tracks.find(t => t.kind === 'local');
          const localWav = await readFile(path.join(recordingDir, localTrack.file));
          const offset = Math.max(4800, localTrack.frames - 14000);
          const power330 = tonePower(localWav, 48000, 330, offset, 12000);
          const power440 = tonePower(localWav, 48000, 440, offset, 12000);
          assert.ok(power330 > 0.0005, `${name}: second destination carries 330 Hz mic signal`);
          assert.ok(power440 < power330 * 0.01, `${name}: first destination 440 Hz oscillator never routed to second-destination sender`);
        }

        report.regressionControls.push({
          name,
          collector: regCollector,
          autoAdmissionState: regRow.callEvidence.state
        });
        console.log(`PASS regression control: ${name} (auto-admission=${regRow.callEvidence.state})`);
      } finally {
        await regCaller.close();
        await regRemote.close();
      }
    }
  }

  // Mandatory negative control: pure synthetic OscillatorNode -> GainNode ->
  // MediaStreamAudioDestinationNode (even with a captured microphone modulating
  // GainNode.gain AudioParam) + live incoming remote audio MUST stay non-qualifying.
  {
    const negCallerUrl = `${baseUrl}/replay/synthetic-oscillator/caller`;
    const negRemoteUrl = `${baseUrl}/replay/synthetic-oscillator/remote`;
    const negCaller = await context.newPage();
    const negRemote = await context.newPage();
    negCaller.on('pageerror', error => pageErrors.push(error.message));
    negRemote.on('pageerror', error => pageErrors.push(error.message));

    try {
      await Promise.all([negCaller.goto(negCallerUrl), negRemote.goto(negRemoteUrl)]);
      const syntheticFixture = validatePlatformTopologyFixture({
        schema: 'cassini-platform-topology/v1',
        platform: 'synthetic-oscillator',
        capturedOn: '2026-11-12',
        provenance: 'synthetic-probe',
        call: { localSpeakers: 0, remoteSpeakers: 2 },
        runtime: { usesWebAudio: true, usesAudioWorklet: false, usesWasm: false },
        webAudioGraphs: [
          {
            id: 'graph-0',
            contextState: 'running',
            nodes: [
              { id: 'n0', type: 'getUserMediaAudio', origin: 'microphone' },
              { id: 'n1', type: 'MediaStreamAudioSourceNode' },
              { id: 'n2', type: 'OscillatorNode', origin: 'synthetic' },
              { id: 'n3', type: 'GainNode' },
              { id: 'n4', type: 'MediaStreamAudioDestinationNode' }
            ],
            edges: [
              { from: 'n0', to: 'n1', kind: 'stream' },
              { from: 'n1', to: 'n3', kind: 'param', output: 0 },
              { from: 'n2', to: 'n3', kind: 'audio', output: 0, input: 0 },
              { from: 'n3', to: 'n4', kind: 'audio', output: 0, input: 0 }
            ],
            outputTrackOrigin: 'synthetic'
          }
        ],
        peers: [
          {
            peer: 0,
            connectionState: 'connected',
            dataChannelCount: 0,
            transceivers: [
              {
                slot: 0,
                kind: 'audio',
                direction: 'sendrecv',
                currentDirection: 'sendrecv',
                sender: {
                  kind: 'audio',
                  label: 'MediaStreamAudioDestinationNode',
                  lineage: 'webaudio-synthetic',
                  webAudioGraphId: 'graph-0',
                  webAudioDestNodeId: 'n4',
                  cloned: false,
                  packetsProgressing: true
                },
                receiver: {
                  kind: 'audio',
                  label: 'remote audio',
                  mixing: 'mixed',
                  speakersOnTrack: 2,
                  speakersOverTime: [2],
                  csrc: { present: false, distinctCount: 0 },
                  ssrc: { present: true, distinctCount: 1 },
                  packetsProgressing: true
                }
              }
            ]
          }
        ]
      });

      await replayFixtureInPages(negCaller, negRemote, syntheticFixture, [[550, 880]]);
      const before = await waitRtcTopology(negCaller, { sending: 'any' });
      const topology = summarizeRtcTopology(before, await waitRtcTopology(negCaller, { previous: before, sending: 'any' }));
      assert.equal(topology.sendingPeers, 0, 'oscillator + param-modulated WebAudio sender has zero captured mic lineage');
      assert.equal(topology.receivingPeers, 1, 'remote RTP packets really arrive');

      const dumpedNeg = await dumpPlatformTopologyFixture(negCaller, {
        platform: 'synthetic-oscillator',
        capturedOn: '2026-11-12',
        provenance: 'synthetic-probe',
        localSpeakers: 0,
        remoteSpeakers: 2,
        receiverMixing: 'mixed'
      });
      assert.equal(dumpedNeg.webAudioGraphs[0].outputTrackOrigin, 'synthetic');
      assert.equal(dumpedNeg.peers[0].transceivers[0].sender.lineage, 'webaudio-synthetic');

      await negCaller.waitForFunction(() => globalThis.__cassiniCallEvidenceV1?.read().revision >= 3);
      const negTabId = await worker.evaluate(
        async targetUrl => (await chrome.tabs.query({})).find(tab => tab.url === targetUrl).id,
        negCallerUrl
      );
      const negBarrier = controlEvents.length;
      controlPeer.write(JSON.stringify({
        type: 'configure',
        capabilities: ['call-signals-v1', 'verified-auto-admission-v1']
      }) + '\n');
      const negRow = await waitForControlEvent(
        () => controlEvents
          .slice(negBarrier)
          .filter(e => e.type === 'callSignals')
          .flatMap(e => e.tabs)
          .find(tab => tab.tabId === negTabId),
        'negative control callSignals snapshot'
      );
      const negCollector = await negCaller.evaluate(() => {
        const v = globalThis.__cassiniCallEvidenceV1.read();
        return {
          cohortQualified: v.cohortQualified,
          capturedSendingPeers: v.capturedSendingPeers,
          receivedPeers: v.receivedPeers
        };
      });
      assert.equal(negCollector.cohortQualified, false, 'synthetic WebAudio sender must never qualify');
      assert.equal(negCollector.capturedSendingPeers, 0);
      assert.equal(negRow.callEvidence.state, 'unknown');
      assert.equal(negRow.callEvidence.callGeneration, null);
      report.negativeControl = { collector: negCollector, autoAdmissionState: negRow.callEvidence.state };
      console.log('PASS negative control: synthetic oscillator + AudioParam mic modulation stays unverified (cohortQualified=false)');
    } finally {
      await negCaller.close();
      await negRemote.close();
    }
  }

  assert.deepEqual(pageErrors, []);
} finally {
  await mkdir(path.join(root, 'artifacts'), { recursive: true });
  await writeFile(path.join(root, 'artifacts/platform-topology-replay.json'), JSON.stringify(report, null, 2));
  await context?.close();
  controlPeer?.destroy();
  await new Promise(resolve => controlServer.close(resolve));
  await new Promise(resolve => httpServer.close(resolve));
  await rm(temporary, { recursive: true, force: true });
}
