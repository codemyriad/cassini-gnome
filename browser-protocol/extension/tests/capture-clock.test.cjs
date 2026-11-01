const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../capture.js'), 'utf8');

async function exercise(timestamp) {
  const originals = new Map(['AudioContext', 'AudioWorkletNode', 'performance'].map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)]));
  let now = 100, node;
  const packets = [], failures = [];
  class GraphNode {
    connect(target) { return target; }
    disconnect() {}
  }
  class Context {
    sampleRate = 48000;
    currentTime = 0;
    state = 'running';
    destination = new GraphNode();
    audioWorklet = { addModule: async () => {} };
    createMediaStreamSource() { return new GraphNode(); }
    createGain() { return Object.assign(new GraphNode(), { gain: { value: 1 } }); }
    async resume() { this.state = "running"; }
    async close() { this.state = 'closed'; }
    getOutputTimestamp() { return timestamp; }
  }
  class Worklet extends GraphNode {
    connect(target) { queueMicrotask(()=>this.port.onmessage?.({data:{ready:true}})); return target; }
    constructor() {
      super(); node = this;
      this.port = { onmessage: null, postMessage: message => { if(message === "stop") this.port.onmessage({ data: { stopped: true } }); } };
    }
  }
  Object.defineProperty(globalThis, 'performance', { configurable: true, value: { timeOrigin: 100000, now: () => now } });
  globalThis.AudioContext = Context;
  globalThis.AudioWorkletNode = Worklet;
  try {
    const { capture } = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);
    const recorder = await capture({ stream: {}, id: 'test', label: 'Test', kind: 'local', origin: 100100,
      workletURL: 'unused', send: async packet => packets.push(packet), fail: message => failures.push(message) });
    // resume() completed while currentTime was still zero; output starts 800 ms later.
    now = 900;
    node.port.onmessage({ data: { pcm: new ArrayBuffer(4096), frame: 0 } });
    await new Promise(resolve => setImmediate(resolve));
    // Delayed delivery of the next batch must not stretch the recorded sample timeline.
    now = 1900;
    node.port.onmessage({ data: { pcm: new ArrayBuffer(4096), frame: 2048 } });
    await recorder.stop();
    assert.deepEqual(failures, []);
    return packets.filter(packet => packet.type === 'audio');
  } finally {
    for (const [key, descriptor] of originals) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor);
      else delete globalThis[key];
    }
  }
}

test('output timestamp calibration preserves delayed AudioContext startup', async () => {
  const packets = await exercise({ contextTime: 0.02, performanceTime: 880 });
  assert.ok(Math.abs(packets[0].offsetMs - 760) < 0.001, `first batch starts at ${packets[0].offsetMs} ms`);
  assert.ok(Math.abs(packets[1].offsetMs - packets[0].offsetMs - 2048 / 48) < 0.001);
});

test('zero output timestamp falls back once to first-batch arrival minus its duration', async () => {
  const packets = await exercise({ contextTime: 0, performanceTime: 0 });
  assert.ok(Math.abs(packets[0].offsetMs - (800 - 2048 / 48)) < 0.001, `first batch starts at ${packets[0].offsetMs} ms`);
  assert.ok(Math.abs(packets[1].offsetMs - packets[0].offsetMs - 2048 / 48) < 0.001);
});

test('ten-second source budget pipelines four requests, counts drops and leaves other sources running', async () => {
  const originals = new Map(['AudioContext', 'AudioWorkletNode'].map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)]));
  const nodes = [], packets = [], failures = [], warnings = [];
  let release, outstanding = 0, maximum = 0;
  const gate = new Promise(resolve => { release = resolve; });
  class GraphNode { connect(target) { return target; } disconnect() {} }
  class Context {
    sampleRate = 48000; state = 'running'; destination = new GraphNode();
    audioWorklet = { addModule: async () => {} };
    createMediaStreamSource() { return new GraphNode(); }
    createGain() { return Object.assign(new GraphNode(), { gain: { value: 1 } }); }
    async resume() { this.state = "running"; } async close() { this.state = 'closed'; }
    getOutputTimestamp() { return { contextTime: 0, performanceTime: 0 }; }
  }
  class Worklet extends GraphNode {
    connect(target) { queueMicrotask(()=>this.port.onmessage?.({data:{ready:true}})); return target; }
    constructor() {
      super(); nodes.push(this);
      this.port = { postMessage: message => { if(message === "stop") this.port.onmessage({ data: { stopped: true } }); } };
    }
  }
  globalThis.AudioContext = Context; globalThis.AudioWorkletNode = Worklet;
  try {
    const { capture } = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);
    const recorder = await capture({ stream: {}, id: 'stalled', label: 'Stalled', kind: 'local', origin: Date.now(), workletURL: '',
      fail: message => failures.push(message), warn: message => warnings.push(message), send: async packet => {
        packets.push(packet);
        if (packet.type === 'audio') { maximum = Math.max(maximum, ++outstanding); await gate; outstanding--; }
      } });
    for (let index = 0; index < 70; index++) nodes[0].port.onmessage({ data: { pcm: new ArrayBuffer(8192 * 2), frame: index * 8192 } });
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(maximum, 4);
    assert.equal(recorder.droppedFrames, 12 * 8192);
    assert.ok(warnings.length > 0);
    const healthyPackets = [];
    const healthy = await capture({ stream: {}, id: 'healthy', label: 'Healthy', kind: 'local', origin: Date.now(), workletURL: '',
      fail: message => failures.push(message), send: async packet => healthyPackets.push(packet) });
    nodes[1].port.onmessage({ data: { pcm: new ArrayBuffer(8192 * 2), frame: 0 } });
    await healthy.stop();
    assert.equal(healthyPackets.filter(packet => packet.type === 'audio').length, 1);
    release(); await recorder.stop();
    assert.equal(packets.filter(packet => packet.type === 'audio').length, 58);
    assert.deepEqual(packets.filter(packet => packet.type === 'audio').map(packet => packet.sequence), Array.from({ length: 58 }, (_, i) => i));
    assert.deepEqual(failures, []);
  } finally {
    release();
    for (const [key, descriptor] of originals) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor);
      else delete globalThis[key];
    }
  }
});
