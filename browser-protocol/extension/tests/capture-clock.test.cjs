const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../capture.js'), 'utf8');

// D61: Date.now() is the wall clock the session origin comes from; performance.now()
// is the document's monotonic clock. timeOrigin is deliberately unrelated to both,
// so a calibration that reads it moves every offset.
async function exercise(timestamp, { timeOrigin = 100000, currentTime = 0, clockFrame = 4800, frames = [0, 2048], spans = 1, retries = [], outputLatency, baseLatency } = {}) {
  const originals = new Map(['AudioContext', 'AudioWorkletNode', 'performance'].map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)]));
  const wall = Date.now;
  let now = 100;
  const nodes = [], packets = [], failures = [], warnings = [], clockRequests = [];
  class GraphNode {
    connect(target) { return target; }
    disconnect() {}
  }
  class Context {
    sampleRate = 48000;
    currentTime = currentTime;
    outputLatency = outputLatency;
    baseLatency = baseLatency;
    state = 'running';
    destination = new GraphNode();
    audioWorklet = { addModule: async () => {} };
    createMediaStreamSource() { return new GraphNode(); }
    createGain() { return Object.assign(new GraphNode(), { gain: { value: 1 } }); }
    async resume() { this.state = "running"; }
    async close() { this.state = 'closed'; }
  }
  if (timestamp !== undefined) Context.prototype.getOutputTimestamp = () => timestamp;
  class Worklet extends GraphNode {
    connect(target) { queueMicrotask(()=>this.port.onmessage?.({data:{ready:true}})); return target; }
    constructor() {
      super(); nodes.push(this);
      this.port = { onmessage: null, postMessage: message => {
        if (message === "stop") this.port.onmessage({ data: { stopped: true } });
        // A 10 ms round trip: the render frame is read at its midpoint.
        if (message === "clock") { clockRequests.push(now); now += 5; queueMicrotask(() => { now += 5; this.port.onmessage?.({ data: { clockFrame } }); }); }
      } };
    }
  }
  Object.defineProperty(globalThis, 'performance', { configurable: true, value: { timeOrigin, now: () => now } });
  Date.now = () => 100000 + now;
  globalThis.AudioContext = Context;
  globalThis.AudioWorkletNode = Worklet;
  try {
    const { capture } = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);
    const sharedContext = spans > 1 ? new Context() : undefined, recorders = [];
    for (let span = 0; span < spans; span++) {
      recorders.push(await capture({ stream: {}, id: 'test-' + span, label: 'Test', kind: 'local', origin: 100100, sharedContext,
        workletURL: 'unused', send: async packet => packets.push(packet), fail: message => failures.push(message), warn: message => warnings.push(message) }));
      now += 250;
    }
    // Delayed delivery of a batch must not stretch the recorded sample timeline.
    for (const frame of frames) {
      now += 1000;
      for (const node of nodes) node.port.onmessage({ data: { pcm: new ArrayBuffer(4096), frame } });
      await new Promise(resolve => setImmediate(resolve));
    }
    for (const recorder of recorders) await recorder.stop();
    assert.deepEqual(failures, []);
    assert.deepEqual(warnings, retries, 'no span aborted or retried unless expected');
    return { audio: packets.filter(packet => packet.type === 'audio'), tracks: packets.filter(packet => packet.type === 'track'), clockRequests };
  } finally {
    Date.now = wall;
    for (const [key, descriptor] of originals) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor);
      else delete globalThis[key];
    }
  }
}
const close = (actual, expected, message) => assert.ok(Math.abs(actual - expected) < 0.001, `${message}: ${actual}, expected ${expected}`);
const clockFields = track => ({ clockMethod: track.clockMethod, clockUncertaintyMs: track.clockUncertaintyMs, clockClampMs: track.clockClampMs });

test('output timestamp calibration preserves delayed AudioContext startup and declares its method', async () => {
  const { audio, tracks, clockRequests } = await exercise({ contextTime: 0.02, performanceTime: 880 });
  close(audio[0].offsetMs, 760, 'first batch start');
  close(audio[1].offsetMs - audio[0].offsetMs, 2048 / 48, 'render-frame spacing');
  // ceil(map 1 ms + one 128-frame quantum at 48 kHz); no clamp, no round trip.
  assert.deepEqual(clockFields(tracks[0]), { clockMethod: 'outputTimestamp', clockUncertaintyMs: 4, clockClampMs: undefined });
  assert.equal('clockClampMs' in tracks[0], false);
  assert.deepEqual(clockRequests, []);
});

test('without a positive output timestamp the worklet round trip calibrates with a strictly larger bound', async () => {
  for (const timestamp of [undefined, { contextTime: 0, performanceTime: 0 }]) {
    const { audio, tracks, clockRequests } = await exercise(timestamp, { currentTime: 0.1, frames: [4800, 6848] });
    // Frame 4800 read at 105 ms (midpoint of 100..110); the origin maps to 100 ms.
    close(audio[0].offsetMs, 105 - 100 - 100 + 4800 / 48, 'round-trip start');
    close(audio[1].offsetMs - audio[0].offsetMs, 2048 / 48, 'render-frame spacing');
    assert.equal(clockRequests.length, 1, 'one round trip per AudioContext');
    // ceil(map 1 + quantum 2.67 + half the 10 ms round trip).
    assert.deepEqual(clockFields(tracks[0]), { clockMethod: 'roundTrip', clockUncertaintyMs: 9, clockClampMs: undefined });
    const stamped = await exercise({ contextTime: 0.02, performanceTime: 880 });
    assert.ok(tracks[0].clockUncertaintyMs > stamped.tracks[0].clockUncertaintyMs, 'the round trip adds its measured half');
  }
});

test('both methods place a render frame at the instant it reaches the output, within their bounds', async () => {
  // One device: frame 4800 is rendered at 105 ms and reaches the output 40 ms later,
  // which is what getOutputTimestamp reports; the callback moves 20 ms of frames at once.
  const device = { currentTime: 0.1, frames: [4800, 6848], outputLatency: 0.04, baseLatency: 0.02 };
  const stamped = await exercise({ contextTime: 0.1, performanceTime: 145 }, device), trip = await exercise(undefined, device);
  close(stamped.audio[0].offsetMs, 45, 'output timestamp start');
  close(trip.audio[0].offsetMs, 45, 'round trip start, moved by outputLatency');
  // ceil(map 1 + the 20 ms callback in place of one quantum + half the 10 ms round trip).
  assert.deepEqual(clockFields(trip.tracks[0]), { clockMethod: 'roundTrip', clockUncertaintyMs: 26, clockClampMs: undefined });
  assert.ok(Math.abs(stamped.audio[0].offsetMs - trip.audio[0].offsetMs) <= stamped.tracks[0].clockUncertaintyMs + trip.tracks[0].clockUncertaintyMs);
});

test('a start raised to the origin is never a rounding hair below it', async () => {
  // These exact values make first -464 ms, and (offset + 464) + start == -2.8e-14 ms.
  const { audio, tracks } = await exercise({ contextTime: 171.723, performanceTime: 171106.75 }, { currentTime: 12108 / 48000, frames: [12108, 14156] });
  assert.equal(tracks[0].clockClampMs, 464);
  assert.equal(audio[0].offsetMs, 0);
  close(audio[1].offsetMs - audio[0].offsetMs, 2048 / 48, 'spacing stays exact');
});

test('a start more than a day before the origin is a fault, not a clamp', async () => {
  const { tracks } = await exercise({ contextTime: 90000, performanceTime: 1 }, { frames: [], retries: ['Audio timing fell before the recording start.'] });
  assert.deepEqual(tracks, [], 'no track is declared with a clamp past the schema maximum');
});

test('a chunk timed before the origin aborts its span visibly instead of clamping', async () => {
  // Frame 0 precedes the declared currentTime: no real worklet delivers it.
  const { audio } = await exercise(undefined, { currentTime: 0.1, frames: [0], retries: Array(2).fill('Audio timing fell before the recording start.') });
  // Reported once as the retry warning and once when Stop finds the span failed.
  assert.deepEqual(audio, []);
});

test('the track is declared calibrated, before any PCM is admitted', async () => {
  const { tracks } = await exercise({ contextTime: 0.02, performanceTime: 880 }, { frames: [] });
  assert.equal(tracks.length, 1);
  assert.equal(typeof tracks[0].clockUncertaintyMs, 'number');
});

test('a calibration before the origin raises the whole span once and records clockClampMs', async () => {
  // performanceTime 120 - origin 100 - contextTime 50 ms = -30 ms at currentTime 0.
  const { audio, tracks } = await exercise({ contextTime: 0.05, performanceTime: 120 }, { frames: [0, 2048, 4096] });
  assert.equal(tracks[0].clockClampMs, 30);
  assert.ok(tracks[0].clockUncertaintyMs >= tracks[0].clockClampMs, 'a clamp proves an error at least that large');
  close(audio[0].offsetMs, 0, 'the raised first chunk');
  for (let i = 1; i < audio.length; i++) close(audio[i].offsetMs - audio[i - 1].offsetMs, 2048 / 48, 'spacing stays exact');
  assert.ok(audio.every(packet => packet.offsetMs >= 0));
});

test('a skewed document timeOrigin leaves every offset unchanged', async () => {
  for (const timestamp of [{ contextTime: 0.02, performanceTime: 880 }, undefined]) {
    const clocks = { currentTime: 0.1, frames: [4800, 6848] };
    const straight = await exercise(timestamp, clocks), skewed = await exercise(timestamp, { ...clocks, timeOrigin: 100500 });
    assert.deepEqual(skewed.audio.map(packet => packet.offsetMs), straight.audio.map(packet => packet.offsetMs));
    assert.deepEqual(skewed.tracks.map(clockFields), straight.tracks.map(clockFields));
  }
});

test('a second span on a shared context repeats every clock field without recalibrating', async () => {
  // Frame 4800 at 105 ms with currentTime 0: the start is 95 ms before the origin.
  const { audio, tracks, clockRequests } = await exercise(undefined, { spans: 2 });
  assert.equal(tracks.length, 2);
  assert.equal(clockRequests.length, 1);
  assert.deepEqual(clockFields(tracks[0]), { clockMethod: 'roundTrip', clockUncertaintyMs: 95, clockClampMs: 95 });
  assert.deepEqual(clockFields(tracks[1]), clockFields(tracks[0]));
  const byId = id => audio.filter(packet => packet.id === id).map(packet => packet.offsetMs);
  assert.deepEqual(byId('test-1'), byId('test-0'), 'one calibration places both spans on one timeline');
  close(byId('test-0')[0], 0, 'frame 0 of the calibration raised by 95 ms');
});

test('ten-second source budget pipelines four requests, counts drops and leaves other sources running', async () => {
  const originals = new Map(['AudioContext', 'AudioWorkletNode'].map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)]));
  const nodes = [], packets = [], failures = [], warnings = [], last = new Map();
  // D61: no offset is ever negative or earlier than its span's previous one.
  const ordered = packet => {
    if (packet.type !== 'audio') return;
    assert.ok(packet.offsetMs >= 0 && packet.offsetMs >= (last.get(packet.id) ?? 0), `offset ${packet.offsetMs}`);
    last.set(packet.id, packet.offsetMs);
  };
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
      this.port = { postMessage: message => {
        if (message === "stop") this.port.onmessage({ data: { stopped: true } });
        if (message === "clock") queueMicrotask(() => this.port.onmessage({ data: { clockFrame: 0 } }));
      } };
    }
  }
  globalThis.AudioContext = Context; globalThis.AudioWorkletNode = Worklet;
  try {
    const { capture } = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);
    const recorder = await capture({ stream: {}, id: 'stalled', label: 'Stalled', kind: 'local', origin: Date.now(), workletURL: '',
      fail: message => failures.push(message), warn: message => warnings.push(message), send: async packet => {
        packets.push(packet); ordered(packet);
        if (packet.type === 'audio') { maximum = Math.max(maximum, ++outstanding); await gate; outstanding--; }
      } });
    for (let index = 0; index < 70; index++) nodes[0].port.onmessage({ data: { pcm: new ArrayBuffer(8192 * 2), frame: index * 8192 } });
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(maximum, 4);
    assert.equal(recorder.droppedFrames, 12 * 8192);
    assert.ok(warnings.length > 0);
    const healthyPackets = [];
    const healthy = await capture({ stream: {}, id: 'healthy', label: 'Healthy', kind: 'local', origin: Date.now(), workletURL: '',
      fail: message => failures.push(message), send: async packet => { ordered(packet); healthyPackets.push(packet); } });
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
