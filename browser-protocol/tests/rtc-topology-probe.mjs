// Test-only and manual-probe observation installed before a page creates peers
// or WebAudio contexts. No media, SDP, ICE credentials, fingerprints, IPs,
// track/device IDs, URLs, DOM text or participant names enter exported reports.
import { mkdir, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

export const PLATFORM_TOPOLOGY_SCHEMA = 'cassini-platform-topology/v1';

export const ALLOWED_TRACK_LABELS = Object.freeze([
  'MediaStreamAudioDestinationNode',
  'remote audio',
  'remote video',
  'microphone',
  'camera',
  'display audio',
  'display video',
  'synthetic audio',
  'synthetic video',
  'opaque-local-audio',
  'opaque-local-video',
  'opaque-remote-audio',
  'opaque-remote-video',
  'unlabeled'
]);

export const ALLOWED_WEBAUDIO_NODE_TYPES = Object.freeze([
  'getUserMediaAudio',
  'getDisplayMediaAudio',
  'MediaStreamAudioSourceNode',
  'MediaElementAudioSourceNode',
  'MediaStreamTrackAudioSourceNode',
  'MediaStreamAudioDestinationNode',
  'AudioDestinationNode',
  'GainNode',
  'BiquadFilterNode',
  'DynamicsCompressorNode',
  'AnalyserNode',
  'DelayNode',
  'ConvolverNode',
  'WaveShaperNode',
  'StereoPannerNode',
  'PannerNode',
  'ChannelSplitterNode',
  'ChannelMergerNode',
  'IIRFilterNode',
  'ScriptProcessorNode',
  'AudioWorkletNode',
  'OscillatorNode',
  'AudioBufferSourceNode',
  'ConstantSourceNode',
  'AudioNode'
]);

const ALLOWED_PROVENANCE = new Set(['live-probe', 'synthetic-probe', 'incident-observation']);
const ALLOWED_NODE_ORIGINS = new Set(['microphone', 'display', 'remote', 'media-element', 'synthetic', 'unknown']);
const ALLOWED_EDGE_KINDS = new Set(['stream', 'audio', 'param']);
const ALLOWED_GRAPH_OUTPUT_ORIGINS = new Set([
  'microphone', 'display', 'remote', 'media-element', 'synthetic', 'mixed-microphone-display', 'none'
]);
const ALLOWED_CONNECTION_STATES = new Set(['new', 'connecting', 'connected', 'disconnected', 'failed', 'closed']);
const ALLOWED_DIRECTIONS = new Set(['sendrecv', 'sendonly', 'recvonly', 'inactive', 'stopped']);
const ALLOWED_LINEAGES = new Set([
  'microphone', 'microphone-clone', 'webaudio-microphone', 'webaudio-synthetic',
  'webaudio-media-element', 'webaudio-display', 'display', 'camera', 'synthetic', 'unknown'
]);
const ALLOWED_MIXING = new Set(['single', 'mixed', 'switched', 'unknown']);
const LABEL_SET = new Set(ALLOWED_TRACK_LABELS);
const NODE_TYPE_SET = new Set(ALLOWED_WEBAUDIO_NODE_TYPES);

const FORBIDDEN_PATTERNS = [
  /\bv=0\b/i,
  /\ba=(?:fingerprint|ice-ufrag|ice-pwd|candidate|ssrc|msid|crypto):/i,
  /\bc=IN\s+IP[46]\b/i,
  /\b(?:https?|wss?|turns?|stuns?|file|chrome-extension|data):/i,
  /\b(?:\d{1,3}\.){3}\d{1,3}\b/,
  /\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b/i,
  /[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/
];

function exactKeys(value, label, required, optional = []) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error(`${label} must be an object`);
  }
  const allowed = new Set([...required, ...optional]);
  for (const key of required) {
    if (!Object.prototype.hasOwnProperty.call(value, key)) {
      throw new Error(`${label} missing required property "${key}"`);
    }
  }
  for (const key of Object.keys(value)) {
    if (!allowed.has(key)) {
      throw new Error(`${label} has unexpected property "${key}"`);
    }
  }
}

function assertBoundedInt(value, label, min, max) {
  if (!Number.isSafeInteger(value) || value < min || value > max) {
    throw new Error(`${label} must be an integer in [${min}, ${max}]`);
  }
}

function assertValidIsoDate(value, label) {
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(value)) {
    throw new Error(`${label} must be YYYY-MM-DD`);
  }
  const [year, month, day] = value.split('-').map(Number);
  const date = new Date(Date.UTC(year, month - 1, day));
  if (date.getUTCFullYear() !== year || date.getUTCMonth() + 1 !== month || date.getUTCDate() !== day) {
    throw new Error(`${label} is not a valid calendar date: ${value}`);
  }
}

function scanNoSecrets(value, pointer = '$', depth = 0) {
  if (depth > 8) {
    throw new Error(`${pointer} exceeds maximum nesting depth`);
  }
  if (typeof value === 'string') {
    if (value.length > 128) {
      throw new Error(`${pointer} exceeds maximum string length`);
    }
    for (const pattern of FORBIDDEN_PATTERNS) {
      if (pattern.test(value)) {
        throw new Error(`${pointer} contains forbidden secret, address, identifier or URL pattern`);
      }
    }
    return;
  }
  if (Array.isArray(value)) {
    if (value.length > 256) {
      throw new Error(`${pointer} exceeds maximum array length`);
    }
    value.forEach((item, index) => scanNoSecrets(item, `${pointer}[${index}]`, depth + 1));
    return;
  }
  if (value && typeof value === 'object') {
    const entries = Object.entries(value);
    if (entries.length > 64) {
      throw new Error(`${pointer} exceeds maximum object property count`);
    }
    for (const [key, child] of entries) {
      scanNoSecrets(key, `${pointer}.<key>`, depth + 1);
      scanNoSecrets(child, `${pointer}.${key}`, depth + 1);
    }
  }
}

export function validatePlatformTopologyFixture(fixture) {
  exactKeys(fixture, 'fixture', [
    'schema', 'platform', 'capturedOn', 'provenance', 'call', 'runtime', 'webAudioGraphs', 'peers'
  ]);
  scanNoSecrets(fixture);
  if (fixture.schema !== PLATFORM_TOPOLOGY_SCHEMA) {
    throw new Error(`Unsupported platform topology schema: ${fixture.schema}`);
  }
  if (typeof fixture.platform !== 'string' || fixture.platform.length < 2 || fixture.platform.length > 64 ||
      !/^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(fixture.platform)) {
    throw new Error(`Invalid platform slug: ${fixture.platform}`);
  }
  assertValidIsoDate(fixture.capturedOn, 'capturedOn');
  if (!ALLOWED_PROVENANCE.has(fixture.provenance)) {
    throw new Error(`Invalid provenance: ${fixture.provenance}`);
  }

  exactKeys(fixture.call, 'call', ['localSpeakers', 'remoteSpeakers']);
  assertBoundedInt(fixture.call.localSpeakers, 'call.localSpeakers', 0, 16);
  if (fixture.call.remoteSpeakers !== null) {
    assertBoundedInt(fixture.call.remoteSpeakers, 'call.remoteSpeakers', 0, 64);
  }

  exactKeys(fixture.runtime, 'runtime', ['usesWebAudio', 'usesAudioWorklet', 'usesWasm']);
  for (const flag of ['usesWebAudio', 'usesAudioWorklet', 'usesWasm']) {
    if (typeof fixture.runtime[flag] !== 'boolean') {
      throw new Error(`runtime.${flag} must be boolean`);
    }
  }

  if (!Array.isArray(fixture.webAudioGraphs) || fixture.webAudioGraphs.length > 32) {
    throw new Error('webAudioGraphs must be an array of at most 32 items');
  }
  if (fixture.webAudioGraphs.length > 0 && !fixture.runtime.usesWebAudio) {
    throw new Error('runtime.usesWebAudio must be true when webAudioGraphs is non-empty');
  }
  const graphNodeMaps = new Map();
  let sawWorkletNode = false;
  fixture.webAudioGraphs.forEach((graph, index) => {
    const label = `webAudioGraphs[${index}]`;
    exactKeys(graph, label, ['id', 'contextState', 'nodes', 'edges', 'outputTrackOrigin']);
    if (graph.id !== `graph-${index}`) {
      throw new Error(`${label}.id must be canonical "graph-${index}"`);
    }
    if (!['suspended', 'running', 'closed'].includes(graph.contextState)) {
      throw new Error(`${label}.contextState invalid: ${graph.contextState}`);
    }
    if (!ALLOWED_GRAPH_OUTPUT_ORIGINS.has(graph.outputTrackOrigin)) {
      throw new Error(`${label}.outputTrackOrigin invalid: ${graph.outputTrackOrigin}`);
    }
    if (!Array.isArray(graph.nodes) || graph.nodes.length < 1 || graph.nodes.length > 128) {
      throw new Error(`${label}.nodes must contain 1..128 nodes`);
    }
    const nodesById = new Map();
    graph.nodes.forEach((node, nodeIndex) => {
      const nodeLabel = `${label}.nodes[${nodeIndex}]`;
      exactKeys(node, nodeLabel, ['id', 'type'], ['origin', 'inputs', 'outputs']);
      if (node.id !== `n${nodeIndex}`) {
        throw new Error(`${nodeLabel}.id must be canonical "n${nodeIndex}"`);
      }
      if (!NODE_TYPE_SET.has(node.type)) {
        throw new Error(`${nodeLabel}.type not allowlisted: ${node.type}`);
      }
      if (node.type === 'AudioWorkletNode') sawWorkletNode = true;
      if (node.origin !== undefined && !ALLOWED_NODE_ORIGINS.has(node.origin)) {
        throw new Error(`${nodeLabel}.origin invalid: ${node.origin}`);
      }
      if (node.inputs !== undefined) {
        const minInputs = node.type === 'ChannelMergerNode' ? 1 : 0;
        assertBoundedInt(node.inputs, `${nodeLabel}.inputs`, minInputs, 32);
      }
      if (node.outputs !== undefined) {
        const minOutputs = node.type === 'ChannelSplitterNode' ? 1 : 0;
        assertBoundedInt(node.outputs, `${nodeLabel}.outputs`, minOutputs, 32);
      }
      nodesById.set(node.id, node);
    });
    graphNodeMaps.set(graph.id, nodesById);
    if (!Array.isArray(graph.edges) || graph.edges.length > 256) {
      throw new Error(`${label}.edges must contain 0..256 edges`);
    }
    graph.edges.forEach((edge, edgeIndex) => {
      const edgeLabel = `${label}.edges[${edgeIndex}]`;
      if (!edge || typeof edge !== 'object' || Array.isArray(edge) || !ALLOWED_EDGE_KINDS.has(edge.kind)) {
        throw new Error(`${edgeLabel}.kind invalid: ${edge?.kind}`);
      }
      if (edge.kind === 'stream') {
        exactKeys(edge, edgeLabel, ['from', 'to', 'kind']);
      } else if (edge.kind === 'audio') {
        exactKeys(edge, edgeLabel, ['from', 'to', 'kind', 'output', 'input']);
        assertBoundedInt(edge.output, `${edgeLabel}.output`, 0, 31);
        assertBoundedInt(edge.input, `${edgeLabel}.input`, 0, 31);
      } else {
        exactKeys(edge, edgeLabel, ['from', 'to', 'kind', 'output']);
        assertBoundedInt(edge.output, `${edgeLabel}.output`, 0, 31);
      }
      const fromNode = nodesById.get(edge.from);
      const toNode = nodesById.get(edge.to);
      if (!fromNode || !toNode) {
        throw new Error(`${edgeLabel} references unknown node id`);
      }
      if (edge.kind !== 'stream') {
        if (fromNode.type === 'MediaStreamAudioDestinationNode' || fromNode.type === 'AudioDestinationNode') {
          throw new Error(`${edgeLabel}.from "${edge.from}" (${fromNode.type}) has no outputs`);
        }
        if (fromNode.outputs !== undefined && edge.output >= fromNode.outputs) {
          throw new Error(`${edgeLabel}.output (${edge.output}) exceeds ${edge.from}.outputs (${fromNode.outputs})`);
        }
        if (edge.kind === 'audio' && toNode.inputs !== undefined && edge.input >= toNode.inputs) {
          throw new Error(`${edgeLabel}.input (${edge.input}) exceeds ${edge.to}.inputs (${toNode.inputs})`);
        }
      }
    });
  });
  if (sawWorkletNode && !fixture.runtime.usesAudioWorklet) {
    throw new Error('runtime.usesAudioWorklet must be true when AudioWorkletNode is present');
  }

  if (!Array.isArray(fixture.peers) || fixture.peers.length > 32) {
    throw new Error('peers must be an array of at most 32 items');
  }
  fixture.peers.forEach((peer, peerIndex) => {
    const peerLabel = `peers[${peerIndex}]`;
    exactKeys(peer, peerLabel, ['peer', 'connectionState', 'dataChannelCount', 'transceivers']);
    if (peer.peer !== peerIndex) {
      throw new Error(`${peerLabel}.peer must be canonical ${peerIndex}`);
    }
    if (!ALLOWED_CONNECTION_STATES.has(peer.connectionState)) {
      throw new Error(`${peerLabel}.connectionState invalid: ${peer.connectionState}`);
    }
    assertBoundedInt(peer.dataChannelCount, `${peerLabel}.dataChannelCount`, 0, 64);
    if (!Array.isArray(peer.transceivers) || peer.transceivers.length > 64) {
      throw new Error(`${peerLabel}.transceivers must contain 0..64 items`);
    }
    peer.transceivers.forEach((slot, slotIndex) => {
      const slotLabel = `${peerLabel}.transceivers[${slotIndex}]`;
      exactKeys(slot, slotLabel, ['slot', 'kind', 'direction', 'currentDirection', 'sender', 'receiver']);
      if (slot.slot !== slotIndex) {
        throw new Error(`${slotLabel}.slot must be canonical ${slotIndex}`);
      }
      if (!['audio', 'video'].includes(slot.kind)) {
        throw new Error(`${slotLabel}.kind invalid: ${slot.kind}`);
      }
      if (!ALLOWED_DIRECTIONS.has(slot.direction)) {
        throw new Error(`${slotLabel}.direction invalid: ${slot.direction}`);
      }
      if (slot.currentDirection !== null && !ALLOWED_DIRECTIONS.has(slot.currentDirection)) {
        throw new Error(`${slotLabel}.currentDirection invalid: ${slot.currentDirection}`);
      }
      if (slot.sender !== null) {
        const senderLabel = `${slotLabel}.sender`;
        exactKeys(slot.sender, senderLabel, [
          'kind', 'label', 'lineage', 'webAudioGraphId', 'webAudioDestNodeId', 'cloned', 'packetsProgressing'
        ]);
        if (!['audio', 'video'].includes(slot.sender.kind)) {
          throw new Error(`${senderLabel}.kind invalid: ${slot.sender.kind}`);
        }
        if (!LABEL_SET.has(slot.sender.label)) {
          throw new Error(`${senderLabel}.label not allowlisted: ${slot.sender.label}`);
        }
        if (!ALLOWED_LINEAGES.has(slot.sender.lineage)) {
          throw new Error(`${senderLabel}.lineage invalid: ${slot.sender.lineage}`);
        }
        if ((slot.sender.webAudioGraphId === null) !== (slot.sender.webAudioDestNodeId === null)) {
          throw new Error(`${senderLabel}.webAudioGraphId and webAudioDestNodeId must both be null or both be set`);
        }
        if (slot.sender.webAudioGraphId !== null) {
          if (slot.sender.kind !== 'audio') {
            throw new Error(`${senderLabel}.kind must be "audio" when webAudioGraphId is set`);
          }
          const targetGraphNodes = graphNodeMaps.get(slot.sender.webAudioGraphId);
          if (!targetGraphNodes) {
            throw new Error(`${senderLabel}.webAudioGraphId references unknown graph "${slot.sender.webAudioGraphId}"`);
          }
          const destNode = targetGraphNodes.get(slot.sender.webAudioDestNodeId);
          if (!destNode) {
            throw new Error(
              `${senderLabel}.webAudioDestNodeId references unknown node "${slot.sender.webAudioDestNodeId}" in "${slot.sender.webAudioGraphId}"`
            );
          }
          if (destNode.type !== 'MediaStreamAudioDestinationNode') {
            throw new Error(
              `${senderLabel}.webAudioDestNodeId "${slot.sender.webAudioDestNodeId}" must reference a MediaStreamAudioDestinationNode, got "${destNode.type}"`
            );
          }
        }
        if (typeof slot.sender.cloned !== 'boolean' || typeof slot.sender.packetsProgressing !== 'boolean') {
          throw new Error(`${senderLabel}.cloned and packetsProgressing must be booleans`);
        }
      }
      if (slot.receiver !== null) {
        const receiverLabel = `${slotLabel}.receiver`;
        exactKeys(slot.receiver, receiverLabel, [
          'kind', 'label', 'mixing', 'speakersOnTrack', 'speakersOverTime', 'csrc', 'ssrc', 'packetsProgressing'
        ]);
        if (!['audio', 'video'].includes(slot.receiver.kind)) {
          throw new Error(`${receiverLabel}.kind invalid: ${slot.receiver.kind}`);
        }
        if (!LABEL_SET.has(slot.receiver.label)) {
          throw new Error(`${receiverLabel}.label not allowlisted: ${slot.receiver.label}`);
        }
        if (!ALLOWED_MIXING.has(slot.receiver.mixing)) {
          throw new Error(`${receiverLabel}.mixing invalid: ${slot.receiver.mixing}`);
        }
        if (slot.receiver.speakersOnTrack !== null) {
          assertBoundedInt(slot.receiver.speakersOnTrack, `${receiverLabel}.speakersOnTrack`, 0, 64);
          if (slot.receiver.mixing === 'single' && slot.receiver.speakersOnTrack > 1) {
            throw new Error(`${receiverLabel}.speakersOnTrack cannot exceed 1 when mixing is "single"`);
          }
        }
        if (!Array.isArray(slot.receiver.speakersOverTime) || slot.receiver.speakersOverTime.length > 128) {
          throw new Error(`${receiverLabel}.speakersOverTime must contain 0..128 samples`);
        }
        slot.receiver.speakersOverTime.forEach((count, sampleIndex) =>
          assertBoundedInt(count, `${receiverLabel}.speakersOverTime[${sampleIndex}]`, 0, 64));
        for (const sourceKind of ['csrc', 'ssrc']) {
          const summary = slot.receiver[sourceKind];
          exactKeys(summary, `${receiverLabel}.${sourceKind}`, ['present', 'distinctCount']);
          if (typeof summary.present !== 'boolean') {
            throw new Error(`${receiverLabel}.${sourceKind}.present must be boolean`);
          }
          assertBoundedInt(summary.distinctCount, `${receiverLabel}.${sourceKind}.distinctCount`, 0, 64);
          if (summary.present !== (summary.distinctCount > 0)) {
            throw new Error(`${receiverLabel}.${sourceKind}.present must match distinctCount > 0`);
          }
        }
        if (typeof slot.receiver.packetsProgressing !== 'boolean') {
          throw new Error(`${receiverLabel}.packetsProgressing must be boolean`);
        }
      }
    });
  });
  return fixture;
}

export function installRtcTopologyProbe() {
  const Native = window.RTCPeerConnection;
  const peers = new Set(), captured = new WeakSet();
  const trackMeta = new WeakMap();
  const peerMeta = new WeakMap();
  const receiverTimeline = new WeakMap();
  const senderPacketBaseline = new WeakMap(), senderProgress = new WeakMap();
  const receiverPacketBaseline = new WeakMap(), receiverProgress = new WeakMap();
  let usesWebAudio = false, usesAudioWorklet = false, usesWasm = false;
  let samplerStarted = false;

  const senders = Native.prototype.getSenders, receivers = Native.prototype.getReceivers;
  const transceivers = Native.prototype.getTransceivers;
  const stats = Native.prototype.getStats;
  const createDataChannel = Native.prototype.createDataChannel;
  const senderGetStats = globalThis.RTCRtpSender?.prototype?.getStats;
  const receiverGetStats = globalThis.RTCRtpReceiver?.prototype?.getStats;
  const getContributingSources = globalThis.RTCRtpReceiver?.prototype?.getContributingSources;
  const getSynchronizationSources = globalThis.RTCRtpReceiver?.prototype?.getSynchronizationSources;

  const devices = navigator?.mediaDevices, gum = devices?.getUserMedia, gdm = devices?.getDisplayMedia;
  if (typeof gum === 'function') {
    devices.getUserMedia = function (...args) {
      return Reflect.apply(gum, this, args).then(stream => {
        if (typeof stream?.getAudioTracks === 'function') {
          stream.getAudioTracks().forEach(track => {
            captured.add(track);
            trackMeta.set(track, { origin: 'microphone', cloned: false, destNode: null });
          });
        }
        if (typeof stream?.getVideoTracks === 'function') {
          stream.getVideoTracks().forEach(track => {
            trackMeta.set(track, { origin: 'camera', cloned: false, destNode: null });
          });
        }
        return stream;
      });
    };
  }
  if (typeof gdm === 'function') {
    devices.getDisplayMedia = function (...args) {
      return Reflect.apply(gdm, this, args).then(stream => {
        const all = typeof stream?.getTracks === 'function' ? stream.getTracks() :
          (typeof stream?.getAudioTracks === 'function' ? stream.getAudioTracks() : []);
        all.forEach(track => trackMeta.set(track, { origin: 'display', cloned: false, destNode: null }));
        return stream;
      });
    };
  }

  // A fixture declares the ground-truth provenance of a track it derived from
  // the microphone by other means (WebAudio, MediaStream.clone), independently
  // of the packaged hook under test.
  window.cassiniRtcDeclareCaptured = track => {
    if (!track) return;
    captured.add(track);
    if (!trackMeta.has(track)) trackMeta.set(track, { origin: 'microphone', cloned: false, destNode: null });
  };

  if (typeof globalThis.MediaStreamTrack?.prototype?.clone === 'function') {
    const clone = MediaStreamTrack.prototype.clone;
    MediaStreamTrack.prototype.clone = function (...args) {
      const result = Reflect.apply(clone, this, args);
      if (captured.has(this)) captured.add(result);
      const prev = trackMeta.get(this);
      if (prev) trackMeta.set(result, { origin: prev.origin, cloned: true, destNode: prev.destNode });
      return result;
    };
  }

  if (typeof globalThis.MediaStream?.prototype?.clone === 'function') {
    const streamClone = MediaStream.prototype.clone;
    MediaStream.prototype.clone = function (...args) {
      const cloned = Reflect.apply(streamClone, this, args);
      try {
        for (const getter of ['getAudioTracks', 'getVideoTracks']) {
          if (typeof this[getter] !== 'function' || typeof cloned[getter] !== 'function') continue;
          const from = this[getter](), to = cloned[getter]();
          if (from.length !== to.length) continue;
          for (let i = 0; i < from.length; i++) {
            if (captured.has(from[i])) captured.add(to[i]);
            const prev = trackMeta.get(from[i]);
            if (prev) trackMeta.set(to[i], { origin: prev.origin, cloned: true, destNode: prev.destNode });
          }
        }
      } catch {}
      return cloned;
    };
  }

  // Observe WASM compilation/instantiation without reading module bytes or URLs.
  if (typeof globalThis.WebAssembly === 'object' && globalThis.WebAssembly !== null) {
    for (const method of ['instantiate', 'instantiateStreaming', 'compile', 'compileStreaming']) {
      const fn = globalThis.WebAssembly[method];
      if (typeof fn === 'function') {
        try {
          globalThis.WebAssembly[method] = function (...args) {
            usesWasm = true;
            return Reflect.apply(fn, this, args);
          };
        } catch {}
      }
    }
    for (const ctor of ['Module', 'Instance']) {
      const Original = globalThis.WebAssembly[ctor];
      if (typeof Original === 'function') {
        try {
          globalThis.WebAssembly[ctor] = new Proxy(Original, {
            construct(target, args, newTarget) {
              usesWasm = true;
              return Reflect.construct(target, args, newTarget);
            }
          });
        } catch {}
      }
    }
  }

  if (typeof globalThis.AudioWorklet?.prototype?.addModule === 'function') {
    const addModule = globalThis.AudioWorklet.prototype.addModule;
    globalThis.AudioWorklet.prototype.addModule = function (...args) {
      usesAudioWorklet = true;
      usesWebAudio = true;
      return Reflect.apply(addModule, this, args);
    };
  }

  for (const [proto, method, origin] of [
    [globalThis.HTMLCanvasElement?.prototype, 'captureStream', 'synthetic'],
    [globalThis.HTMLMediaElement?.prototype, 'captureStream', 'media-element'],
    [globalThis.HTMLMediaElement?.prototype, 'mozCaptureStream', 'media-element']
  ]) {
    const fn = proto?.[method];
    if (typeof fn === 'function' && !fn.__cassiniWrapped) {
      const wrapped = function (...args) {
        const stream = Reflect.apply(fn, this, args);
        try {
          for (const track of stream?.getTracks?.() || []) {
            if (!trackMeta.has(track)) trackMeta.set(track, { origin, cloned: false, destNode: null });
          }
        } catch {}
        return stream;
      };
      wrapped.__cassiniWrapped = true;
      try { proto[method] = wrapped; } catch {}
    }
  }

  for (const ctxCtor of ['AudioContext', 'webkitAudioContext', 'OfflineAudioContext']) {
    const Original = globalThis[ctxCtor];
    if (typeof Original === 'function' && !Original.__cassiniWrapped) {
      try {
        const proxied = new Proxy(Original, {
          construct(target, args, newTarget) {
            usesWebAudio = true;
            return Reflect.construct(target, args, newTarget);
          }
        });
        proxied.__cassiniWrapped = true;
        globalThis[ctxCtor] = proxied;
      } catch {}
    }
  }

  // Privacy-safe WebAudio graph recorder: captures only structural node types,
  // stream origins, and audio/param edges (never processor names, URLs, or PCM).
  const contextGraphs = new Map();
  const contextList = [];
  const paramOwners = new WeakMap();
  const destNodes = new WeakSet();
  const audioParamClass = globalThis.AudioParam;
  const audioNodeClass = globalThis.AudioNode;

  const NODE_INTERFACES = [
    'MediaStreamAudioSourceNode',
    'MediaElementAudioSourceNode',
    'MediaStreamTrackAudioSourceNode',
    'MediaStreamAudioDestinationNode',
    'AudioDestinationNode',
    'GainNode',
    'BiquadFilterNode',
    'DynamicsCompressorNode',
    'AnalyserNode',
    'DelayNode',
    'ConvolverNode',
    'WaveShaperNode',
    'StereoPannerNode',
    'PannerNode',
    'ChannelSplitterNode',
    'ChannelMergerNode',
    'IIRFilterNode',
    'ScriptProcessorNode',
    'AudioWorkletNode',
    'OscillatorNode',
    'AudioBufferSourceNode',
    'ConstantSourceNode'
  ];

  const PARAM_PROPS = [
    'gain', 'frequency', 'detune', 'Q', 'threshold', 'knee', 'ratio',
    'attack', 'release', 'pan', 'delayTime', 'offset', 'playbackRate'
  ];

  function classifyNodeType(node) {
    for (const name of NODE_INTERFACES) {
      const Ctor = globalThis[name];
      if (typeof Ctor === 'function' && node instanceof Ctor) return name;
    }
    return 'AudioNode';
  }

  function ensureGraph(ctx) {
    if (!ctx || typeof ctx !== 'object') return null;
    usesWebAudio = true;
    let graph = contextGraphs.get(ctx);
    if (!graph) {
      graph = { ctx, nodes: [], nodeIds: new WeakMap(), edges: [] };
      contextGraphs.set(ctx, graph);
      if (contextList.length < 32) contextList.push(graph);
    }
    return graph;
  }

  function registerParams(node) {
    if (typeof audioParamClass !== 'function' || !node) return;
    for (const prop of PARAM_PROPS) {
      try {
        const value = node[prop];
        if (value instanceof audioParamClass) paramOwners.set(value, node);
      } catch {}
    }
    try {
      const map = node.parameters;
      if (map && typeof map.forEach === 'function') {
        map.forEach(param => {
          if (param instanceof audioParamClass) paramOwners.set(param, node);
        });
      }
    } catch {}
  }

  function ensureNode(node, explicitOrigin) {
    if (!node || typeof node !== 'object') return null;
    const graph = ensureGraph(node.context);
    if (!graph) return null;
    let id = graph.nodeIds.get(node);
    if (id) return id;
    if (graph.nodes.length >= 128) return null;
    const type = classifyNodeType(node);
    if (type === 'AudioWorkletNode') usesAudioWorklet = true;
    let origin = explicitOrigin;
    if (!origin) {
      if (['OscillatorNode', 'AudioBufferSourceNode', 'ConstantSourceNode'].includes(type)) {
        origin = 'synthetic';
      } else if (type === 'MediaElementAudioSourceNode') {
        origin = 'media-element';
      } else if (type === 'AudioWorkletNode' && node.numberOfInputs === 0) {
        origin = 'synthetic';
      }
    }
    let inputs;
    let outputs;
    if (type === 'ChannelMergerNode' && Number.isSafeInteger(node.numberOfInputs)) {
      inputs = Math.min(32, Math.max(1, node.numberOfInputs));
    } else if (type === 'ChannelSplitterNode' && Number.isSafeInteger(node.numberOfOutputs)) {
      outputs = Math.min(32, Math.max(1, node.numberOfOutputs));
    } else if (type === 'AudioWorkletNode') {
      if (Number.isSafeInteger(node.numberOfInputs) && node.numberOfInputs !== 1) {
        inputs = Math.min(32, Math.max(0, node.numberOfInputs));
      }
      if (Number.isSafeInteger(node.numberOfOutputs) && node.numberOfOutputs !== 1) {
        outputs = Math.min(32, Math.max(0, node.numberOfOutputs));
      }
    }
    id = `n${graph.nodes.length}`;
    const entry = {
      id,
      type,
      ...(origin ? { origin } : {}),
      ...(inputs !== undefined ? { inputs } : {}),
      ...(outputs !== undefined ? { outputs } : {}),
      ref: node
    };
    graph.nodes.push(entry);
    graph.nodeIds.set(node, id);
    registerParams(node);
    if (type === 'MediaStreamAudioDestinationNode') {
      destNodes.add(node);
      try {
        for (const track of node.stream?.getAudioTracks?.() || []) {
          const prev = trackMeta.get(track);
          trackMeta.set(track, { origin: prev?.origin || 'webaudio', cloned: false, destNode: node });
        }
      } catch {}
    }
    return id;
  }

  function addStreamEdge(graph, from, to) {
    if (!graph || !from || !to || graph.edges.length >= 256) return;
    if (graph.edges.some(edge => edge.from === from && edge.to === to && edge.kind === 'stream')) return;
    graph.edges.push({ from, to, kind: 'stream' });
  }

  function addAudioEdge(graph, from, to, output, input) {
    if (!graph || !from || !to || graph.edges.length >= 256) return;
    if (output < 0 || output > 31 || input < 0 || input > 31) return;
    if (graph.edges.some(edge =>
      edge.from === from && edge.to === to && edge.kind === 'audio' &&
      edge.output === output && edge.input === input
    )) return;
    graph.edges.push({ from, to, kind: 'audio', output, input });
  }

  function addParamEdge(graph, from, to, output, paramRef) {
    if (!graph || !from || !to || graph.edges.length >= 256) return;
    if (output < 0 || output > 31) return;
    if (graph.edges.some(edge =>
      edge.from === from && edge.to === to && edge.kind === 'param' &&
      edge.output === output && edge.paramRef === paramRef
    )) return;
    graph.edges.push({ from, to, kind: 'param', output, paramRef });
  }

  function classifyInputTrackOrigin(track, depth = 0) {
    if (!track || depth > 8) return 'unknown';
    const meta = trackMeta.get(track);
    if (meta?.destNode) {
      const nested = computeDestinationOrigin(meta.destNode, depth + 1);
      if (nested !== 'none') return nested;
      return 'unknown';
    }
    if (captured.has(track) || meta?.origin === 'microphone') return 'microphone';
    if (meta?.origin === 'display') return 'display';
    for (const pc of peers) {
      try {
        if (Reflect.apply(receivers, pc, []).some(rx => rx?.track === track)) return 'remote';
      } catch {}
    }
    return 'unknown';
  }

  function registerStreamSourceNode(node, tracks) {
    const graph = ensureGraph(node?.context);
    if (!graph || graph.nodeIds.has(node)) return node;
    const trackOrigins = (tracks || []).map(track => classifyInputTrackOrigin(track, 0));
    const origins = new Set(trackOrigins);
    const allMic = trackOrigins.length > 0 && trackOrigins.every(o => o === 'microphone' || o === 'mixed-microphone-display');
    const anyDisplay = origins.has('display') || origins.has('mixed-microphone-display');
    const streamNodeIds = [];
    let fallbackOrigin;
    if (allMic && graph.nodes.length < 127) {
      const micNodeId = `n${graph.nodes.length}`;
      graph.nodes.push({ id: micNodeId, type: 'getUserMediaAudio', origin: 'microphone', ref: null });
      streamNodeIds.push(micNodeId);
    } else if (allMic) {
      fallbackOrigin = 'microphone';
    }
    if (anyDisplay && graph.nodes.length < 127) {
      const displayNodeId = `n${graph.nodes.length}`;
      graph.nodes.push({ id: displayNodeId, type: 'getDisplayMediaAudio', origin: 'display', ref: null });
      streamNodeIds.push(displayNodeId);
    } else if (anyDisplay) {
      fallbackOrigin = 'display';
    }
    if (!allMic && !anyDisplay) {
      fallbackOrigin = origins.has('remote') ? 'remote' : 'unknown';
    }
    const sourceId = ensureNode(node, fallbackOrigin);
    if (sourceId) {
      for (const streamNodeId of streamNodeIds) addStreamEdge(graph, streamNodeId, sourceId);
    }
    return node;
  }

  let lineageSteps = 0;
  window.cassiniRtcProbeStats = () => ({ lineageSteps });

  // Match extension/rtc-evidence.js and extension/page-hook.js: microphone
  // lineage propagates ONLY through port 0 (output 0 into input 0), whereas
  // display, remote, media-element and synthetic origins propagate across any port.
  function computeDestinationOrigin(destNode, depth = 0) {
    if (!destNode || depth > 8) return 'none';
    const graph = contextGraphs.get(destNode.context);
    const destId = graph?.nodeIds.get(destNode);
    if (!graph || !destId) return 'none';
    const nodeById = new Map();
    for (const entry of graph.nodes) nodeById.set(entry.id, entry);
    const incomingAny = new Map();
    const incomingPort0 = new Map();
    for (const edge of graph.edges) {
      if (edge.kind !== 'audio' && edge.kind !== 'stream') continue;
      let anyList = incomingAny.get(edge.to);
      if (!anyList) { anyList = []; incomingAny.set(edge.to, anyList); }
      anyList.push(edge.from);
      if (edge.kind === 'stream' || (edge.output === 0 && edge.input === 0)) {
        let port0List = incomingPort0.get(edge.to);
        if (!port0List) { port0List = []; incomingPort0.set(edge.to, port0List); }
        port0List.push(edge.from);
      }
    }

    let micPort0Reachable = false;
    {
      const visited = new Set([destId]);
      const queue = [destId];
      let head = 0;
      while (head < queue.length) {
        lineageSteps++;
        const currentId = queue[head++];
        if (nodeById.get(currentId)?.origin === 'microphone') micPort0Reachable = true;
        const upstream = incomingPort0.get(currentId);
        if (!upstream) continue;
        for (const fromId of upstream) {
          if (!visited.has(fromId)) {
            visited.add(fromId);
            queue.push(fromId);
          }
        }
      }
    }

    const anyOrigins = new Set();
    {
      const visited = new Set([destId]);
      const queue = [destId];
      let head = 0;
      while (head < queue.length) {
        lineageSteps++;
        const currentId = queue[head++];
        const origin = nodeById.get(currentId)?.origin;
        if (origin && origin !== 'microphone') anyOrigins.add(origin);
        const upstream = incomingAny.get(currentId);
        if (!upstream) continue;
        for (const fromId of upstream) {
          if (!visited.has(fromId)) {
            visited.add(fromId);
            queue.push(fromId);
          }
        }
      }
    }

    if (micPort0Reachable && anyOrigins.has('display')) return 'mixed-microphone-display';
    if (micPort0Reachable) return 'microphone';
    if (anyOrigins.has('display')) return 'display';
    if (anyOrigins.has('remote')) return 'remote';
    if (anyOrigins.has('media-element')) return 'media-element';
    if (anyOrigins.has('synthetic')) return 'synthetic';
    return 'none';
  }

  function computeGraphOutputOrigin(graph) {
    const streamDests = graph.nodes.filter(entry => entry.type === 'MediaStreamAudioDestinationNode');
    const destEntries = streamDests.length > 0
      ? streamDests
      : graph.nodes.filter(entry => entry.type === 'AudioDestinationNode');
    if (destEntries.length === 0) return 'none';
    const origins = new Set(destEntries.map(entry => entry.ref ? computeDestinationOrigin(entry.ref, 0) : 'none'));
    if (origins.has('mixed-microphone-display') || (origins.has('microphone') && origins.has('display'))) {
      return 'mixed-microphone-display';
    }
    for (const candidate of ['microphone', 'display', 'remote', 'media-element', 'synthetic']) {
      if (origins.has(candidate)) return candidate;
    }
    return 'none';
  }

  function isLiveCapturedMicrophoneTrack(track) {
    if (!track) return false;
    if (captured.has(track)) return true;
    const meta = trackMeta.get(track);
    if (!meta?.destNode) return false;
    const ctxState = meta.destNode.context?.state;
    if (ctxState && ctxState !== 'running') return false;
    const origin = computeDestinationOrigin(meta.destNode, 0);
    return origin === 'microphone' || origin === 'mixed-microphone-display';
  }

  // Only supplied argument slots: an omitted index never reads a page prototype
  // property; non-numbers become -1 (matching WebIDL unsigned long handling in
  // extension/page-hook.js and extension/rtc-evidence.js).
  const argSlot = (args, position) => (position < args.length ? args[position] : undefined);
  const audioIndex = (value, fallback) =>
    value === undefined ? fallback : (typeof value === 'number' ? (value >>> 0) : -1);

  if (typeof audioNodeClass === 'function' && typeof audioNodeClass.prototype?.connect === 'function') {
    const nativeConnect = audioNodeClass.prototype.connect;
    audioNodeClass.prototype.connect = function (...args) {
      const result = Reflect.apply(nativeConnect, this, args);
      try {
        const fromId = ensureNode(this);
        const graph = contextGraphs.get(this.context);
        const target = argSlot(args, 0);
        if (target && typeof audioNodeClass === 'function' && target instanceof audioNodeClass) {
          const output = audioIndex(argSlot(args, 1), 0);
          const input = audioIndex(argSlot(args, 2), 0);
          const toId = ensureNode(target);
          if (output >= 0 && input >= 0) addAudioEdge(graph, fromId, toId, output, input);
        } else if (target && typeof audioParamClass === 'function' && target instanceof audioParamClass) {
          const output = audioIndex(argSlot(args, 1), 0);
          const owner = paramOwners.get(target);
          if (owner && output >= 0) addParamEdge(graph, fromId, ensureNode(owner), output, target);
        }
      } catch {}
      return result;
    };
  }

  if (typeof audioNodeClass === 'function' && typeof audioNodeClass.prototype?.disconnect === 'function') {
    const nativeDisconnect = audioNodeClass.prototype.disconnect;
    audioNodeClass.prototype.disconnect = function (...args) {
      const result = Reflect.apply(nativeDisconnect, this, args);
      try {
        const graph = contextGraphs.get(this.context);
        const fromId = graph?.nodeIds.get(this);
        if (!graph || !fromId) return result;
        if (args.length === 0) {
          graph.edges = graph.edges.filter(edge => edge.kind === 'stream' || edge.from !== fromId);
        } else {
          const first = argSlot(args, 0);
          if (first instanceof audioNodeClass) {
            const toId = graph.nodeIds.get(first);
            const output = args.length > 1 ? audioIndex(argSlot(args, 1), -1) : -1;
            const input = args.length > 2 ? audioIndex(argSlot(args, 2), -1) : -1;
            if (toId) {
              graph.edges = graph.edges.filter(edge => !(
                edge.from === fromId &&
                edge.to === toId &&
                edge.kind === 'audio' &&
                (output < 0 || edge.output === output) &&
                (input < 0 || edge.input === input)
              ));
            }
          } else if (typeof audioParamClass === 'function' && first instanceof audioParamClass) {
            const owner = paramOwners.get(first);
            const toId = owner && graph.nodeIds.get(owner);
            const output = args.length > 1 ? audioIndex(argSlot(args, 1), -1) : -1;
            if (toId) {
              graph.edges = graph.edges.filter(edge => !(
                edge.from === fromId &&
                edge.to === toId &&
                edge.kind === 'param' &&
                (!edge.paramRef || edge.paramRef === first) &&
                (output < 0 || edge.output === output)
              ));
            }
          } else {
            const output = audioIndex(first, -1);
            graph.edges = graph.edges.filter(edge =>
              edge.kind === 'stream' ||
              edge.from !== fromId ||
              (output >= 0 && edge.output !== output)
            );
          }
        }
      } catch {}
      return result;
    };
  }

  for (const proto of [globalThis.BaseAudioContext?.prototype, globalThis.AudioContext?.prototype]) {
    if (!proto) continue;
    for (const method of [
      'createGain', 'createBiquadFilter', 'createDynamicsCompressor', 'createAnalyser',
      'createDelay', 'createConvolver', 'createWaveShaper', 'createStereoPanner',
      'createPanner', 'createChannelSplitter', 'createChannelMerger', 'createIIRFilter',
      'createScriptProcessor', 'createOscillator', 'createBufferSource', 'createConstantSource',
      'createMediaElementSource', 'createMediaStreamDestination'
    ]) {
      const fn = proto[method];
      if (typeof fn !== 'function' || fn.__cassiniWrapped) continue;
      const wrapped = function (...args) {
        const node = Reflect.apply(fn, this, args);
        try { ensureNode(node); } catch {}
        return node;
      };
      wrapped.__cassiniWrapped = true;
      try { proto[method] = wrapped; } catch {}
    }
    if (typeof proto.createMediaStreamSource === 'function' && !proto.createMediaStreamSource.__cassiniWrapped) {
      const fn = proto.createMediaStreamSource;
      const wrapped = function (stream, ...rest) {
        const node = Reflect.apply(fn, this, [stream, ...rest]);
        try { registerStreamSourceNode(node, stream?.getAudioTracks?.() || []); } catch {}
        return node;
      };
      wrapped.__cassiniWrapped = true;
      try { proto.createMediaStreamSource = wrapped; } catch {}
    }
    if (typeof proto.createMediaStreamTrackSource === 'function' && !proto.createMediaStreamTrackSource.__cassiniWrapped) {
      const fn = proto.createMediaStreamTrackSource;
      const wrapped = function (track, ...rest) {
        const node = Reflect.apply(fn, this, [track, ...rest]);
        try { registerStreamSourceNode(node, track ? [track] : []); } catch {}
        return node;
      };
      wrapped.__cassiniWrapped = true;
      try { proto.createMediaStreamTrackSource = wrapped; } catch {}
    }
  }

  for (const name of NODE_INTERFACES) {
    const Original = globalThis[name];
    if (typeof Original !== 'function') continue;
    try {
      globalThis[name] = new Proxy(Original, {
        construct(target, args, newTarget) {
          const node = Reflect.construct(target, args, newTarget);
          try {
            if (name === 'MediaStreamAudioSourceNode') {
              registerStreamSourceNode(node, (args[1]?.mediaStream || node.mediaStream)?.getAudioTracks?.() || []);
            } else if (name === 'MediaStreamTrackAudioSourceNode') {
              registerStreamSourceNode(node, args[1]?.mediaStreamTrack ? [args[1].mediaStreamTrack] : []);
            } else {
              ensureNode(node);
            }
          } catch {}
          return node;
        }
      });
    } catch {}
  }

  if (typeof createDataChannel === 'function') {
    Native.prototype.createDataChannel = function (...args) {
      const channel = Reflect.apply(createDataChannel, this, args);
      const info = peerMeta.get(this);
      if (info && info.dataChannels < 64) info.dataChannels++;
      return channel;
    };
  }

  function sampleReceiverRtpSources() {
    for (const pc of peers) {
      let rxList = [];
      try { rxList = Reflect.apply(receivers, pc, []) || []; } catch { continue; }
      for (const rx of rxList) {
        if (!rx || rx.track?.kind === 'video') continue;
        let state = receiverTimeline.get(rx);
        if (!state) {
          state = {
            csrcSlots: new Map(),
            csrcLastTimestamp: new Map(),
            ssrcSlots: new Map(),
            ssrcLastTimestamp: new Map(),
            speakersOverTime: []
          };
          receiverTimeline.set(rx, state);
        }
        let csrcEntries = [], ssrcEntries = [];
        try {
          if (typeof getContributingSources === 'function') csrcEntries = Reflect.apply(getContributingSources, rx, []) || [];
          else if (typeof rx.getContributingSources === 'function') csrcEntries = rx.getContributingSources() || [];
        } catch {}
        try {
          if (typeof getSynchronizationSources === 'function') ssrcEntries = Reflect.apply(getSynchronizationSources, rx, []) || [];
          else if (typeof rx.getSynchronizationSources === 'function') ssrcEntries = rx.getSynchronizationSources() || [];
        } catch {}

        const countActive = (entries, slots, lastTimestamps) => {
          const activeSlots = new Set();
          let maxTs = -Infinity;
          for (const entry of entries) {
            if (typeof entry?.timestamp === 'number' && entry.timestamp > maxTs) maxTs = entry.timestamp;
          }
          for (const entry of entries) {
            if (entry?.source === undefined || entry?.source === null) continue;
            if (!slots.has(entry.source) && slots.size < 64) slots.set(entry.source, slots.size);
            const slot = slots.get(entry.source);
            if (slot === undefined) continue;
            const ts = typeof entry.timestamp === 'number' ? entry.timestamp : null;
            const prevTs = lastTimestamps.get(entry.source);
            const active = ts === null
              ? prevTs === undefined
              : ((prevTs === undefined || ts > prevTs) && (maxTs - ts <= 1000) &&
                 (entry.audioLevel === undefined || entry.audioLevel > 0));
            if (ts !== null) lastTimestamps.set(entry.source, ts);
            else lastTimestamps.set(entry.source, 0);
            if (active) activeSlots.add(slot);
          }
          return activeSlots.size;
        };

        const activeCsrcs = countActive(csrcEntries, state.csrcSlots, state.csrcLastTimestamp);
        const activeSsrcs = countActive(ssrcEntries, state.ssrcSlots, state.ssrcLastTimestamp);
        const observedActive = activeCsrcs > 0 ? activeCsrcs : activeSsrcs;
        if (observedActive > 0 && state.speakersOverTime.length < 64) {
          if (state.speakersOverTime.length === 0 || state.speakersOverTime.at(-1) !== observedActive) {
            state.speakersOverTime.push(observedActive);
          }
        }
      }
    }
  }

  async function samplePacketProgress() {
    sampleReceiverRtpSources();
    for (const pc of peers) {
      let reportMap;
      try { reportMap = await Reflect.apply(stats, pc, []); } catch { continue; }
      if (!reportMap || typeof reportMap.forEach !== 'function') continue;

      let pcSenders = [], pcReceivers = [];
      try { pcSenders = Reflect.apply(senders, pc, []) || []; } catch {}
      try { pcReceivers = Reflect.apply(receivers, pc, []) || []; } catch {}

      for (const sender of pcSenders) {
        if (!sender) continue;
        let total = null;
        const readSenderStats = typeof senderGetStats === 'function'
          ? () => Reflect.apply(senderGetStats, sender, [])
          : (typeof sender.getStats === 'function' ? () => sender.getStats() : null);
        if (readSenderStats) {
          try {
            const sr = await readSenderStats();
            total = 0;
            sr.forEach(r => { if (r?.type === 'outbound-rtp') total += r.packetsSent || 0; });
          } catch { total = null; }
        }
        if (total === null) {
          total = 0;
          reportMap.forEach(r => {
            if (r?.type === 'outbound-rtp' && (!sender.track || !r.kind || r.kind === sender.track.kind)) {
              total += r.packetsSent || 0;
            }
          });
        }
        const prev = senderPacketBaseline.get(sender);
        if (prev !== undefined && total > prev) senderProgress.set(sender, true);
        else if (!senderProgress.has(sender)) senderProgress.set(sender, false);
        senderPacketBaseline.set(sender, total);
      }

      for (const rx of pcReceivers) {
        if (!rx) continue;
        let total = null;
        const readReceiverStats = typeof receiverGetStats === 'function'
          ? () => Reflect.apply(receiverGetStats, rx, [])
          : (typeof rx.getStats === 'function' ? () => rx.getStats() : null);
        if (readReceiverStats) {
          try {
            const rr = await readReceiverStats();
            total = 0;
            rr.forEach(r => { if (r?.type === 'inbound-rtp') total += r.packetsReceived || 0; });
          } catch { total = null; }
        }
        if (total === null) {
          total = 0;
          reportMap.forEach(r => {
            if (r?.type === 'inbound-rtp' && (!rx.track || !r.kind || r.kind === rx.track.kind)) {
              total += r.packetsReceived || 0;
            }
          });
        }
        const prev = receiverPacketBaseline.get(rx);
        if (prev !== undefined && total > prev) receiverProgress.set(rx, true);
        else if (!receiverProgress.has(rx)) receiverProgress.set(rx, false);
        receiverPacketBaseline.set(rx, total);
      }
    }
  }

  window.RTCPeerConnection = new Proxy(Native, {
    construct(target, args, newTarget) {
      const pc = Reflect.construct(target, args, newTarget);
      peers.add(pc);
      peerMeta.set(pc, { dataChannels: 0 });
      if (typeof pc.addEventListener === 'function') {
        try {
          pc.addEventListener('datachannel', () => {
            const info = peerMeta.get(pc);
            if (info && info.dataChannels < 64) info.dataChannels++;
          });
        } catch {}
      }
      if (!samplerStarted && typeof window.requestAnimationFrame === 'function') {
        samplerStarted = true;
        let lastSampleAt = -Infinity;
        const pump = now => {
          if (now - lastSampleAt >= 200) {
            lastSampleAt = now;
            sampleReceiverRtpSources();
          }
          window.requestAnimationFrame(pump);
        };
        window.requestAnimationFrame(pump);
      }
      return pc;
    }
  });
  if (window.webkitRTCPeerConnection === Native) window.webkitRTCPeerConnection = window.RTCPeerConnection;

  // Study only: export opaque equality classes of standardized native SDP
  // fields, never SDP/credentials/fingerprints/names themselves. Shared server
  // identity alone is explicitly NOT an admission/session-cohort rule.
  const descriptor = Object.getOwnPropertyDescriptor, apply = Reflect.apply;
  const local = descriptor(Native.prototype, 'localDescription')?.get;
  const remote = descriptor(Native.prototype, 'remoteDescription')?.get;
  const sdp = descriptor(globalThis.RTCSessionDescription?.prototype || {}, 'sdp')?.get;
  window.cassiniRtcCohortFacts = (groups = new Map()) => {
    // A same-origin disposable peer window may share this private table for
    // cross-room comparison. Only integer references enter the returned facts.
    const reference = (kind, value) => {
      if (!value || value === '-') return null;
      let values = groups.get(kind); if (!values) { values = new Map(); groups.set(kind, values); }
      if (!values.has(value)) values.set(value, values.size);
      return values.get(value);
    };
    const fields = description => {
      const result = { originSession: [], sessionName: [], iceUsername: [], fingerprint: [], rtcpCname: [], streamId: [] };
      let text; try { text = apply(sdp, description, []); } catch { return result; }
      if (typeof text !== 'string' || text.length > 1048576) return result;
      for (const line of text.split(/\r?\n/)) {
        let kind, value;
        if (line.startsWith('o=')) { kind = 'originSession'; value = line.slice(2).split(/\s+/)[1]; }
        else if (line.startsWith('s=')) { kind = 'sessionName'; value = line.slice(2); }
        else if (line.startsWith('a=ice-ufrag:')) { kind = 'iceUsername'; value = line.slice(12); }
        else if (line.startsWith('a=fingerprint:')) { kind = 'fingerprint'; value = line.slice(14); }
        else if (/^a=ssrc:\d+ cname:/.test(line)) { kind = 'rtcpCname'; value = line.slice(line.indexOf(' cname:') + 7); }
        else if (line.startsWith('a=msid:')) { kind = 'streamId'; value = line.slice(7).split(/\s+/)[0]; }
        if (kind && result[kind].length < 32) { const group = reference(kind, value); if (group !== null && !result[kind].includes(group)) result[kind].push(group); }
      }
      return result;
    };
    return {
      notAdmissionRule: true,
      scope: 'native applied descriptions; integer equality classes only',
      peers: [...peers].slice(0, 16).map((pc, peer) => {
        let a = null, b = null; try { a = apply(local, pc, []); b = apply(remote, pc, []); } catch {}
        return { peer, local: fields(a), remote: fields(b) };
      })
    };
  };

  window.cassiniRtcTopology = async () => {
    await samplePacketProgress();
    return Promise.all([...peers].map(async (pc, index) => {
      const live = track => track?.kind === 'audio' && track.readyState === 'live';
      const row = {
        peer: index,
        connected: pc.connectionState === 'connected',
        liveCapturedSenders: Reflect.apply(senders, pc, []).filter(sender =>
          live(sender.track) && sender.track.enabled && !sender.track.muted &&
          isLiveCapturedMicrophoneTrack(sender.track)).length,
        liveReceivers: Reflect.apply(receivers, pc, []).filter(receiver => live(receiver.track)).length,
        outboundPackets: 0,
        inboundPackets: 0
      };
      (await Reflect.apply(stats, pc, [])).forEach(report => {
        if ((report.kind ?? report.mediaType) !== 'audio') return;
        if (report.type === 'outbound-rtp') row.outboundPackets += report.packetsSent || 0;
        if (report.type === 'inbound-rtp') row.inboundPackets += report.packetsReceived || 0;
      });
      return row;
    }));
  };

  window.cassiniSampleRtcTopology = samplePacketProgress;

  function senderLineage(track, graphInfoByCtx) {
    if (!track) return { lineage: 'unknown', webAudioGraphId: null, webAudioDestNodeId: null, cloned: false };
    const meta = trackMeta.get(track);
    if (meta?.destNode) {
      const graphInfo = graphInfoByCtx.get(meta.destNode.context) || null;
      const destNodeId = graphInfo?.nodeIds.get(meta.destNode) ?? null;
      const graphId = destNodeId ? graphInfo.id : null;
      const origin = computeDestinationOrigin(meta.destNode, 0);
      const map = {
        'microphone': 'webaudio-microphone',
        'mixed-microphone-display': 'webaudio-microphone',
        'display': 'webaudio-display',
        'media-element': 'webaudio-media-element',
        'synthetic': 'webaudio-synthetic',
        'remote': 'webaudio-synthetic',
        'none': 'unknown'
      };
      return {
        lineage: map[origin] || 'unknown',
        webAudioGraphId: graphId,
        webAudioDestNodeId: destNodeId,
        cloned: !!meta.cloned
      };
    }
    if (captured.has(track) || meta?.origin === 'microphone') {
      return {
        lineage: meta?.cloned ? 'microphone-clone' : 'microphone',
        webAudioGraphId: null,
        webAudioDestNodeId: null,
        cloned: !!meta?.cloned
      };
    }
    if (meta?.origin === 'display') {
      return { lineage: 'display', webAudioGraphId: null, webAudioDestNodeId: null, cloned: !!meta.cloned };
    }
    if (meta?.origin === 'camera') {
      return { lineage: 'camera', webAudioGraphId: null, webAudioDestNodeId: null, cloned: !!meta.cloned };
    }
    if (meta?.origin === 'synthetic') {
      return { lineage: 'synthetic', webAudioGraphId: null, webAudioDestNodeId: null, cloned: !!meta.cloned };
    }
    if (meta?.origin === 'media-element') {
      return { lineage: 'webaudio-media-element', webAudioGraphId: null, webAudioDestNodeId: null, cloned: !!meta.cloned };
    }
    return { lineage: 'unknown', webAudioGraphId: null, webAudioDestNodeId: null, cloned: !!meta?.cloned };
  }

  function sanitizeLabel(track, role, lineage) {
    const kind = track?.kind === 'video' ? 'video' : 'audio';
    const raw = typeof track?.label === 'string' ? track.label.trim() : '';
    if (raw === '') return 'unlabeled';
    if (raw === 'MediaStreamAudioDestinationNode') return 'MediaStreamAudioDestinationNode';
    if (raw === 'remote audio' || raw === 'remote video') return raw;
    if (role === 'sender') {
      if (lineage === 'microphone' || lineage === 'microphone-clone') return 'microphone';
      if (lineage === 'camera') return 'camera';
      if (lineage === 'display' || lineage === 'webaudio-display') {
        return kind === 'video' ? 'display video' : 'display audio';
      }
      if (lineage.startsWith('webaudio-') || lineage === 'synthetic') {
        return kind === 'video' ? 'synthetic video' : 'synthetic audio';
      }
      return kind === 'video' ? 'opaque-local-video' : 'opaque-local-audio';
    }
    if (typeof track?.id === 'string' && track.id.length > 0 && raw === track.id) {
      return kind === 'video' ? 'remote video' : 'remote audio';
    }
    return kind === 'video' ? 'opaque-remote-video' : 'opaque-remote-audio';
  }

  window.cassiniRtcPlatformFixture = async (options = {}) => {
    await samplePacketProgress();
    if (options.sampleWindowMs > 0 && typeof window.requestAnimationFrame === 'function' &&
        typeof performance?.now === 'function') {
      const stopAt = performance.now() + options.sampleWindowMs;
      while (performance.now() < stopAt) {
        await new Promise(resolve => window.requestAnimationFrame(resolve));
        sampleReceiverRtpSources();
      }
      await samplePacketProgress();
    }

    const graphInfoByCtx = new Map();
    const webAudioGraphs = contextList.slice(0, 32).filter(graph => graph.nodes.length > 0).map((graph, index) => {
      const id = `graph-${index}`;
      const exportedNodeIds = new Set(graph.nodes.map(n => n.id));
      graphInfoByCtx.set(graph.ctx, { id, nodeIds: graph.nodeIds, exportedNodeIds });
      const state = ['suspended', 'running', 'closed'].includes(graph.ctx.state) ? graph.ctx.state : 'running';
      const serializedEdges = [];
      const seenEdgeKeys = new Set();
      for (const edge of graph.edges) {
        if (!exportedNodeIds.has(edge.from) || !exportedNodeIds.has(edge.to)) continue;
        let serialized;
        let key;
        if (edge.kind === 'stream') {
          serialized = { from: edge.from, to: edge.to, kind: 'stream' };
          key = `stream:${edge.from}->${edge.to}`;
        } else if (edge.kind === 'audio') {
          serialized = { from: edge.from, to: edge.to, kind: 'audio', output: edge.output, input: edge.input };
          key = `audio:${edge.from}:${edge.output}->${edge.to}:${edge.input}`;
        } else {
          serialized = { from: edge.from, to: edge.to, kind: 'param', output: edge.output };
          key = `param:${edge.from}:${edge.output}->${edge.to}`;
        }
        if (!seenEdgeKeys.has(key)) {
          seenEdgeKeys.add(key);
          serializedEdges.push(serialized);
        }
      }
      return {
        id,
        contextState: state,
        nodes: graph.nodes.map(({ id: nodeId, type, origin, inputs, outputs }) => ({
          id: nodeId,
          type,
          ...(origin !== undefined ? { origin } : {}),
          ...(inputs !== undefined ? { inputs } : {}),
          ...(outputs !== undefined ? { outputs } : {})
        })),
        edges: serializedEdges,
        outputTrackOrigin: computeGraphOutputOrigin(graph)
      };
    });

    const peerRows = [...peers].slice(0, 32).map((pc, peerIndex) => {
      const connectionState = ['new', 'connecting', 'connected', 'disconnected', 'failed', 'closed']
        .includes(pc.connectionState) ? pc.connectionState : (pc.signalingState === 'closed' ? 'closed' : 'new');
      const dataChannelCount = peerMeta.get(pc)?.dataChannels || 0;
      let rawSlots = [];
      if (typeof transceivers === 'function') {
        try { rawSlots = Reflect.apply(transceivers, pc, []) || []; } catch {}
      }
      if (rawSlots.length === 0) {
        const ps = (Reflect.apply(senders, pc, []) || []).map(sender => ({
          direction: 'sendonly', currentDirection: 'sendonly', sender, receiver: null
        }));
        const pr = (Reflect.apply(receivers, pc, []) || []).map(receiver => ({
          direction: 'recvonly', currentDirection: 'recvonly', sender: null, receiver
        }));
        rawSlots = [...ps, ...pr];
      }

      const slotRows = rawSlots.slice(0, 64).map((slot, slotIndex) => {
        const senderTrack = slot.sender?.track || null;
        const receiverTrack = slot.receiver?.track || null;
        const kind = (receiverTrack?.kind || senderTrack?.kind) === 'video' ? 'video' : 'audio';
        const direction = ['sendrecv', 'sendonly', 'recvonly', 'inactive', 'stopped'].includes(slot.direction)
          ? slot.direction : 'inactive';
        const currentDirection = slot.currentDirection === null
          ? null
          : (['sendrecv', 'sendonly', 'recvonly', 'inactive', 'stopped'].includes(slot.currentDirection)
            ? slot.currentDirection : null);

        const includeSender = !!senderTrack && ['sendrecv', 'sendonly'].includes(currentDirection || direction);
        const includeReceiver = !!receiverTrack && ['sendrecv', 'recvonly'].includes(currentDirection || direction);

        let senderDesc = null;
        if (includeSender) {
          const info = senderLineage(senderTrack, graphInfoByCtx);
          senderDesc = {
            kind: senderTrack.kind === 'video' ? 'video' : 'audio',
            label: sanitizeLabel(senderTrack, 'sender', info.lineage),
            lineage: info.lineage,
            webAudioGraphId: info.webAudioGraphId,
            webAudioDestNodeId: info.webAudioDestNodeId,
            cloned: info.cloned,
            packetsProgressing: senderProgress.get(slot.sender) === true
          };
        }

        let receiverDesc = null;
        if (includeReceiver) {
          const rxKind = receiverTrack.kind === 'video' ? 'video' : 'audio';
          const timeline = receiverTimeline.get(slot.receiver);
          const csrcCount = timeline?.csrcSlots.size || 0;
          const ssrcCount = timeline?.ssrcSlots.size || 0;
          let speakersOverTime = timeline ? [...timeline.speakersOverTime] : [];
          const maxConcurrent = speakersOverTime.length > 0 ? Math.max(...speakersOverTime) : 0;
          let mixing = 'unknown';
          let speakersOnTrack = null;
          if (csrcCount >= 2) {
            mixing = maxConcurrent >= 2 ? 'mixed' : 'switched';
            speakersOnTrack = Math.max(csrcCount, maxConcurrent);
          } else if (ssrcCount >= 2) {
            mixing = maxConcurrent >= 2 ? 'mixed' : 'switched';
            speakersOnTrack = Math.max(ssrcCount, maxConcurrent);
          } else if (rxKind === 'audio' && options.receiverMixing &&
                     ['single', 'mixed', 'switched'].includes(options.receiverMixing)) {
            mixing = options.receiverMixing;
            speakersOnTrack = mixing === 'single'
              ? 1
              : (Number.isSafeInteger(options.remoteSpeakers) ? options.remoteSpeakers : null);
            if (speakersOnTrack !== null && speakersOverTime.length === 0) {
              speakersOverTime = [speakersOnTrack];
            }
          } else if (csrcCount === 1) {
            mixing = 'single';
            speakersOnTrack = 1;
          }
          receiverDesc = {
            kind: rxKind,
            label: sanitizeLabel(receiverTrack, 'receiver', 'unknown'),
            mixing,
            speakersOnTrack,
            speakersOverTime,
            csrc: { present: csrcCount > 0, distinctCount: csrcCount },
            ssrc: { present: ssrcCount > 0, distinctCount: ssrcCount },
            packetsProgressing: receiverProgress.get(slot.receiver) === true
          };
        }

        return {
          slot: slotIndex,
          kind,
          direction,
          currentDirection,
          sender: senderDesc,
          receiver: receiverDesc
        };
      });

      return {
        peer: peerIndex,
        connectionState,
        dataChannelCount,
        transceivers: slotRows
      };
    });

    const hasMicSender = peerRows.some(peer => peer.transceivers.some(slot =>
      slot.sender && ['microphone', 'microphone-clone', 'webaudio-microphone'].includes(slot.sender.lineage)));
    const localSpeakers = Number.isSafeInteger(options.localSpeakers)
      ? options.localSpeakers
      : (hasMicSender ? 1 : 0);
    const remoteSpeakers = Number.isSafeInteger(options.remoteSpeakers) ? options.remoteSpeakers : null;

    return {
      schema: 'cassini-platform-topology/v1',
      platform: typeof options.platform === 'string' ? options.platform : 'unknown-platform',
      capturedOn: typeof options.capturedOn === 'string' ? options.capturedOn : new Date().toISOString().slice(0, 10),
      provenance: ['live-probe', 'synthetic-probe', 'incident-observation'].includes(options.provenance)
        ? options.provenance : 'live-probe',
      call: { localSpeakers, remoteSpeakers },
      runtime: {
        usesWebAudio: usesWebAudio || webAudioGraphs.length > 0,
        usesAudioWorklet,
        usesWasm
      },
      webAudioGraphs,
      peers: peerRows
    };
  };
}

export function summarizeRtcTopology(before, after) {
  const progressing = after.map(row => {
    const previous = before.find(value => value.peer === row.peer);
    return {
      ...row,
      sending: !!previous && row.connected && row.liveCapturedSenders > 0 && row.outboundPackets > previous.outboundPackets,
      receiving: !!previous && row.connected && row.liveReceivers > 0 && row.inboundPackets > previous.inboundPackets
    };
  });
  return {
    peers: progressing,
    samePeerConnection: progressing.some(row => row.sending && row.receiving),
    sendingPeers: progressing.filter(row => row.sending).length,
    receivingPeers: progressing.filter(row => row.receiving).length
  };
}

// getStats is asynchronous. Await each sample before testing it, and return
// that exact sample. An async waitForFunction predicate can resolve its first
// Promise to false instead of polling the eventual state.
export async function waitRtcTopology(page, { previous = null, sending = 'none', receiving = true } = {}) {
  await (page.bringToFront ? page : page.page()).bringToFront(); // Disposable fixture only.
  return page.evaluate(async ({ previous, sending, receiving }) => {
    const deadline = performance.now() + 30000;
    let latest = [];
    while (performance.now() < deadline) {
      const rows = await cassiniRtcTopology();
      latest = rows;
      const grows = (row, key) => row[key] > (previous ? previous.find(value => value.peer === row.peer)?.[key] ?? Infinity : 0);
      const received = rows.some(row => row.connected && grows(row, 'inboundPackets'));
      const sent = sending === 'none' || rows.some(row => row.connected && grows(row, 'outboundPackets') &&
        (sending !== 'captured' || row.liveCapturedSenders > 0));
      if ((!receiving || received) && sent) return rows;
      await new Promise(resolve => requestAnimationFrame(resolve));
    }
    throw Error('Timed out waiting for real RTC packet progress: ' + JSON.stringify(latest));
  }, { previous, sending, receiving });
}

// Compare only opaque integer groups produced with one private in-browser table.
// Shared fields are candidate measurements, never proof of an interactive session.
export function summarizeRtcCohortComparison(first, second) {
  const fields = ['originSession', 'sessionName', 'iceUsername', 'fingerprint', 'rtcpCname', 'streamId'];
  const values = (facts, field) => new Set(facts.peers.flatMap(peer => peer.remote[field]));
  return {
    notAdmissionRule: true,
    scope: 'same backend, different disposable rooms; shared private equality table',
    first,
    second,
    sharedRemoteGroups: Object.fromEntries(fields.map(field => {
      const other = values(second, field);
      return [field, [...values(first, field)].filter(value => other.has(value))];
    }))
  };
}

export async function dumpPlatformTopologyFixture(page, { outPath = null, ...options } = {}) {
  await (page.bringToFront ? page : page.page?.())?.bringToFront?.();
  const fixture = validatePlatformTopologyFixture(
    await page.evaluate(opts => cassiniRtcPlatformFixture(opts), {
      sampleWindowMs: 0,
      ...options
    })
  );
  if (outPath) {
    await mkdir(path.dirname(outPath), { recursive: true });
    await writeFile(outPath, JSON.stringify(fixture, null, 2) + '\n', 'utf8');
  }
  return fixture;
}

export function parseProbeCliArgs(argv) {
  const parsed = {
    platform: null,
    capturedOn: new Date().toISOString().slice(0, 10),
    out: null,
    launch: null,
    cdp: null,
    sampleMs: 2000,
    localSpeakers: null,
    remoteSpeakers: null,
    receiverMixing: null,
    help: false
  };
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];
    if (arg === '--help' || arg === '-h') { parsed.help = true; continue; }
    const next = () => {
      if (i + 1 >= argv.length) throw new Error(`Missing value for ${arg}`);
      return argv[++i];
    };
    if (arg === '--platform') parsed.platform = next();
    else if (arg === '--date') parsed.capturedOn = next();
    else if (arg === '--out') parsed.out = next();
    else if (arg === '--launch') parsed.launch = next();
    else if (arg === '--cdp') parsed.cdp = next();
    else if (arg === '--sample-ms') parsed.sampleMs = Number(next());
    else if (arg === '--local-speakers') parsed.localSpeakers = Number(next());
    else if (arg === '--remote-speakers') parsed.remoteSpeakers = Number(next());
    else if (arg === '--receiver-mixing') parsed.receiverMixing = next();
    else throw new Error(`Unknown option: ${arg}`);
  }
  if (parsed.help) return parsed;
  if (!parsed.platform || !/^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(parsed.platform)) {
    throw new Error('--platform <slug> (e.g. ms-teams, google-meet, zoom-web) is required');
  }
  assertValidIsoDate(parsed.capturedOn, '--date');
  if (Boolean(parsed.launch) === Boolean(parsed.cdp)) {
    throw new Error('Specify exactly one of --launch <url> or --cdp <http://127.0.0.1:port>');
  }
  if (parsed.launch) {
    const target = new URL(parsed.launch);
    if (!['http:', 'https:'].includes(target.protocol)) {
      throw new Error('--launch must be an http(s) URL');
    }
  }
  if (parsed.cdp) {
    const endpoint = new URL(parsed.cdp);
    if (!['127.0.0.1', 'localhost', '[::1]'].includes(endpoint.hostname)) {
      throw new Error('--cdp must point to a loopback browser endpoint');
    }
  }
  if (!Number.isSafeInteger(parsed.sampleMs) || parsed.sampleMs < 100 || parsed.sampleMs > 60000) {
    throw new Error('--sample-ms must be an integer in [100, 60000]');
  }
  if (parsed.localSpeakers !== null) assertBoundedInt(parsed.localSpeakers, '--local-speakers', 0, 16);
  if (parsed.remoteSpeakers !== null) assertBoundedInt(parsed.remoteSpeakers, '--remote-speakers', 0, 64);
  if (parsed.receiverMixing !== null && !ALLOWED_MIXING.has(parsed.receiverMixing)) {
    throw new Error('--receiver-mixing must be one of: single, mixed, switched, unknown');
  }
  parsed.out ||= path.join('tests', 'fixtures', 'platforms', `${parsed.platform}-${parsed.capturedOn}.json`);
  return parsed;
}

async function runCli(argv) {
  const args = parseProbeCliArgs(argv);
  if (args.help) {
    process.stdout.write(
      'Usage: node tests/rtc-topology-probe.mjs --platform <slug> (--launch <url> | --cdp <http://127.0.0.1:9222>) [--out <file>] [--sample-ms <ms>]\n'
    );
    return;
  }
  const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || 'playwright');
  let browser, owned = false;
  try {
    let page;
    if (args.launch) {
      browser = await chromium.launch({ headless: false });
      owned = true;
      const context = await browser.newContext();
      await context.addInitScript(installRtcTopologyProbe);
      page = await context.newPage();
      await page.goto(args.launch);
      process.stderr.write('Join the call manually in the opened browser window, then press Enter here to sample and dump the sanitized fixture...\n');
      await new Promise(resolve => process.stdin.once('data', resolve));
    } else {
      browser = await chromium.connectOverCDP(args.cdp);
      const pages = browser.contexts().flatMap(ctx => ctx.pages());
      page = pages.at(-1);
      if (!page) throw new Error('No open tab found on CDP browser');
      const hasProbe = await page.evaluate(() => typeof window.cassiniRtcPlatformFixture === 'function');
      if (!hasProbe) {
        throw new Error('Active tab was opened without installRtcTopologyProbe; use --launch so the probe hooks RTCPeerConnection/WebAudio from document start');
      }
    }
    const fixture = await dumpPlatformTopologyFixture(page, {
      outPath: args.out,
      platform: args.platform,
      capturedOn: args.capturedOn,
      provenance: 'live-probe',
      sampleWindowMs: args.sampleMs,
      ...(args.localSpeakers !== null ? { localSpeakers: args.localSpeakers } : {}),
      ...(args.remoteSpeakers !== null ? { remoteSpeakers: args.remoteSpeakers } : {}),
      ...(args.receiverMixing ? { receiverMixing: args.receiverMixing } : {})
    });
    process.stdout.write(`Wrote validated fixture (${fixture.peers.length} peer(s), ${fixture.webAudioGraphs.length} WebAudio graph(s)) to ${args.out}\n`);
  } finally {
    if (owned) await browser?.close();
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  runCli(process.argv.slice(2)).catch(error => {
    process.stderr.write(`${error.message}\n`);
    process.exit(1);
  });
}
