const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../capture.js'), 'utf8');

test('PCM base64 is byte-exact across conversion blocks and large buffers', async () => {
  const { base64 } = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);
  for (const size of [0, 1, 2, 3, 8191, 8192, 8193, 16384, 262145]) {
    const bytes = Uint8Array.from({ length: size }, (_, i) => (i * 73 + 19) % 256);
    assert.equal(base64(bytes.buffer), Buffer.from(bytes).toString('base64'), `${size} bytes`);
  }
});

// D61: Date.now() is the wall clock the session origin comes from; performance.now()
// is the document's monotonic clock. timeOrigin is deliberately unrelated to both,
// so a calibration that reads it moves every offset.
async function exercise(timestamp, { timeOrigin = 100000, currentTime = 0, clockFrame = 4800, frames = [0, 2048], spans = 1, retries = [], outputLatency, baseLatency } = {}) {
  const originals = new Map(['AudioContext', 'AudioWorkletNode', 'performance'].map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)]));
  const wall = Date.now;
  let now = 100;
  const nodes = [], packets = [], failures = [], warnings = [], clockRequests = [], contexts = [];
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
    constants = [];
    constructor() { contexts.push(this); }
    createMediaStreamSource() { return new GraphNode(); }
    createGain() { return Object.assign(new GraphNode(), { gain: { value: 1 } }); }
    createConstantSource() {
      const constant = Object.assign(new GraphNode(), { offset: { value: 1 }, started: false, targets: [] });
      constant.connect = target => { constant.targets.push(target); return target; };
      constant.start = () => { constant.started = true; };
      this.constants.push(constant);
      return constant;
    }
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
    return { audio: packets.filter(packet => packet.type === 'audio'), tracks: packets.filter(packet => packet.type === 'track'), clockRequests, contexts };
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

// D68.1: Chromium suspends a context whose output stays all zeros for 30 s onto a timer that can
// run far slower than real time. Each capture context, a page's shared one included, gets exactly
// one inaudible constant into its destination, started, and never into the worklet's input.
test('each capture context keeps real time with one inaudible constant into its destination', async () => {
  for (const [label, options] of [['its own context', {}], ['a shared context with two spans', { spans: 2 }]]) {
    const { contexts } = await exercise(undefined, options);
    assert.equal(contexts.length, 1, label);
    const [context] = contexts;
    assert.equal(context.constants.length, 1, `${label}: one constant per context`);
    const [constant] = context.constants;
    assert.equal(constant.offset.value, 6e-8, `${label}: -144 dBFS, below one 16-bit step`);
    assert.equal(constant.targets.length, 1, `${label}: one connection`);
    assert.equal(constant.targets[0], context.destination, `${label}: into the destination itself`);
    assert.ok(constant.started, `${label}: started`);
  }
});

// D39 installers replace an installed companion only with a strictly newer one, so the D68.1
// capture fix shipped as 0.2.13, the D61.1 stall re-anchor as 0.2.14, and D61.1 steady drift
// (no re-anchor for a steady rate) ships as 0.2.15 or later.
test('the companion that keeps capture on real time and re-anchors only a stall is 0.2.15 or later', () => {
  const { version } = JSON.parse(require('node:fs').readFileSync(require('node:path').join(__dirname, '..', 'manifest.json'), 'utf8'));
  const [major, minor, patch] = version.split('.').map(Number);
  assert.ok(major > 0 || minor > 2 || (minor === 2 && patch >= 15), `manifest version ${version}`);
  assert.match(source, /export function keepRealTime\(context\)/, 'capture.js keeps each context on real time');
  assert.match(source, /export const STALL_STEP_MS = 50, STALL_AGREE_MS = 20, STALL_CONFIRM_MS = 100, STALL_CENTER = 0.002, STALL_SPREAD = 6, STALL_SLOPE = 0.2, STALL_STUTTER = 0.05;/, 'capture.js re-anchors a render stall, not drift');
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
    createConstantSource() { return Object.assign(new GraphNode(), { offset: { value: 1 }, start() {} }); }
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

// D61.1: a scripted device for render stalls. `level` is the render instant minus the render
// position (ms). Rendering moves the wall clock and the render position together; a stall moves
// only the wall clock, so it raises the level by the rendered time it lost. Each rendered chunk
// restamps getOutputTimestamp (the audio thread's pair); a stall leaves the last stamp in place.
// Truth for a frame is the level when it was rendered plus its position: that is where the
// session timeline (origin 100000 = performance 0 here) places it.
async function device(run, { stamps = true, outputLatency = 0, retries = [] } = {}) {
  const originals = new Map(['AudioContext', 'AudioWorkletNode', 'performance'].map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)]));
  const wall = Date.now;
  const dev = { ct: 0.1, level: 1000, outputLatency, stamps, state: 'running', noise: () => 0, extra: 0, suspendMs: 0 };
  let now = dev.level + dev.ct * 1000;
  const nodes = [], packets = [], warnings = [], failures = [], contexts = [], timers = [];
  const restamp = () => {
    dev.stamp = dev.stamps ? { contextTime: dev.ct, performanceTime: dev.level + dev.ct * 1000 + dev.outputLatency * 1000 + dev.noise() + dev.extra } : { contextTime: 0, performanceTime: 0 };
    dev.extra = 0;
  };
  restamp();
  class GraphNode { connect(target) { return target; } disconnect() {} }
  class Context {
    sampleRate = 48000; baseLatency = 0; destination = new GraphNode(); listeners = new Set();
    audioWorklet = { addModule: async () => {} };
    constructor() { contexts.push(this); }
    get currentTime() { return dev.ct; }
    get state() { return dev.state; }
    get outputLatency() { return dev.outputLatency; }
    createMediaStreamSource() { return new GraphNode(); }
    createGain() { return Object.assign(new GraphNode(), { gain: { value: 1 } }); }
    createConstantSource() { return Object.assign(new GraphNode(), { offset: { value: 1 }, start() {} }); }
    addEventListener(_name, fn) { this.listeners.add(fn); }
    removeEventListener(_name, fn) { this.listeners.delete(fn); }
    getOutputTimestamp() { return { ...dev.stamp }; }
    // A suspension lasts suspendMs of wall time; the device then stamps again.
    async resume() { if (dev.state === 'suspended') { now += dev.suspendMs; dev.level += dev.suspendMs; dev.state = 'running'; restamp(); } }
    async close() {}
  }
  class Worklet extends GraphNode {
    connect(target) { queueMicrotask(() => this.port.onmessage?.({ data: { ready: true } })); return target; }
    constructor() {
      super(); nodes.push(this);
      Object.assign(this, { armed: false, next: 0, stopped: false, chunks: [], queued: [] });
      this.port = { onmessage: null, postMessage: message => {
        if (message === 'start') { this.armed = true; this.next = Math.round(dev.ct * 48000); }
        if (message === 'clock') queueMicrotask(() => this.port.onmessage?.({ data: { clockFrame: Math.round(dev.ct * 48000) } }));
        if (message === 'stop') { this.stopped = true; this.port.onmessage?.({ data: { stopped: true } }); }
      } };
    }
  }
  Object.defineProperty(globalThis, 'performance', { configurable: true, value: { timeOrigin: 5, now: () => now } });
  Date.now = () => 100000 + now;
  globalThis.AudioContext = Context; globalThis.AudioWorkletNode = Worklet;
  const tick = () => new Promise(resolve => setImmediate(resolve));
  try {
    const { capture } = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);
    let shared;
    const rig = {
      dev, nodes, packets, warnings, failures, timers, tick, contexts,
      get now() { return now; },
      async start(id, { own = false, sourceTrackId } = {}) {
        shared ??= new Context();
        const recorder = await capture({ stream: {}, id, sourceTrackId, label: id, kind: 'local', origin: 100000, workletURL: '', reportOutcomes: true,
          sharedContext: own ? undefined : shared, send: async packet => {
            // These long clock simulations assert placement and ordering, not PCM contents.
            // Release the encoded silence once sent instead of retaining hours of audio.
            const { pcm, ...metadata } = packet;
            if (pcm) metadata.frames = Buffer.byteLength(pcm, 'base64') / 2;
            packets.push(metadata);
          },
          fail: message => failures.push(message), warn: (message, details) => warnings.push(details?.kind ?? message),
          schedule: (fn, ms) => { timers.push(fn); return timers.length; }, cancel: () => {}, retryDelayMs: 0 });
        const requested = now;
        return Object.assign(recorder, { requested, node: nodes.at(-1) });
      },
      // Render one 8192-frame batch on every armed worklet; `queue(node)` keeps it from the page.
      // `stamp(fresh)` replaces the device's stamp for this batch before any chunk is delivered,
      // so the readings its chunks trigger see it (a stale or frozen stamp).
      async render({ queue = () => false, drift = 0, stamp } = {}) {
        now += 8192 / 48;
        dev.ct += 8192 / 48000;
        dev.level += drift;
        restamp();
        if (stamp) dev.stamp = stamp(dev.stamp);
        for (const node of nodes) {
          if (!node.armed || node.stopped) continue;
          const chunk = { frame: node.next, truth: dev.level + node.next / 48 + dev.outputLatency * 1000 };
          node.next += 8192;
          if (queue(node)) node.queued.push(chunk); else rig.deliver(node, chunk);
        }
        await tick();
      },
      deliver(node, chunk) {
        node.chunks.push(chunk);
        node.port.onmessage?.({ data: { pcm: new ArrayBuffer(8192 * 2), frame: chunk.frame } });
      },
      async flushQueued() { for (const node of nodes) for (const chunk of node.queued.splice(0)) rig.deliver(node, chunk); await tick(); },
      // After a renderer stall, rendering catches up faster than real time: the page reads a
      // backlog of batches within a millisecond, and the level falls by nearly a batch each.
      async catchUp() {
        now += 0.5; dev.ct += 8192 / 48000; dev.level -= 8192 / 48 - 0.5;
        restamp();
        for (const node of nodes) if (node.armed && !node.stopped) { rig.deliver(node, { frame: node.next, truth: NaN }); node.next += 8192; }
        await tick();
      },
      // Rendering stops: the wall clock goes on, frames and stamps do not.
      stall(ms) { now += ms; dev.level += ms; },
      audio(id) { return packets.filter(packet => packet.type === 'audio' && packet.id === id); },
      // Offset deltas beyond render-frame spacing, i.e. the jumps a host writes as gaps.
      jumps(id) {
        const chunks = rig.audio(id);
        return chunks.slice(1).map((chunk, i) => chunk.offsetMs - chunks[i].offsetMs - 8192 / 48).filter(delta => Math.abs(delta) > 0.001);
      },
    };
    await run(rig);
    assert.deepEqual(failures, []);
    assert.deepEqual(warnings, retries, 'D61.1 adds no warning: the host records the jump');
  } finally {
    Date.now = wall;
    for (const [key, descriptor] of originals) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor);
      else delete globalThis[key];
    }
  }
}
// Each sent chunk of `id` against the truth of the chunk its worklet delivered in that order.
function placement(rig, recorder, id) {
  const sent = rig.audio(id);
  return sent.map((packet, i) => packet.offsetMs - recorder.node.chunks[i].truth);
}
const STEP_BOUND = 50;
const exact = (errors, message, within = 0.001) => assert.ok(errors.every(error => Math.abs(error) < within), `${message}: ${errors.map(e => e.toFixed(3)).join(', ')}`);

test('D61.1: a steady device places every chunk exactly as before, stamps fresh or constant', async () => {
  await device(async rig => {
    const a = await rig.start('A');
    for (let i = 0; i < 20; i++) await rig.render();
    await a.stop();
    exact(placement(rig, a, 'A'), 'fresh stamps on a steady level');
    assert.deepEqual(rig.jumps('A'), []);
  });
  // A stamp that never moves after calibration: every reading is stale, offsets are today's.
  await device(async rig => {
    const a = await rig.start('A');
    const frozen = { ...rig.dev.stamp };
    rig.dev.noise = () => 0;
    // Every batch's readings see the calibration stamp, while the device's own stamp has moved on.
    for (let i = 0; i < 20; i++) await rig.render({ stamp: () => ({ ...frozen }) });
    await a.stop();
    exact(placement(rig, a, 'A'), 'constant stamp');
    assert.deepEqual(rig.jumps('A'), []);
    assert.equal(rig.audio('A').length, 20);
  });
});

test('D61.1: ordinary jitter and drift never re-anchor (2 h at +-3 ms and 100 ppm; +-15 ms)', async () => {
  let seed = 7;
  const random = () => (seed = (seed * 1103515245 + 12345) % 2147483648) / 2147483648;
  for (const [jitter, ppm, chunks] of [[3, 100, Math.ceil(7200 * 48000 / 8192)], [15, 0, 3000], [15, -100, 3000]]) {
    await device(async rig => {
      rig.dev.noise = () => (random() * 2 - 1) * jitter;
      const a = await rig.start('A');
      for (let i = 0; i < chunks; i++) await rig.render({ drift: 8192 / 48 * ppm / 1e6 });
      await a.stop();
      assert.equal(rig.audio('A').length, chunks);
      assert.deepEqual(rig.jumps('A'), [], `jitter ${jitter} ms, drift ${ppm} ppm: no anchor`);
    });
  }
});

test('D61.1: a stall that holds re-anchors once, by the stamp step, from the stalled frame', async () => {
  await device(async rig => {
    const a = await rig.start('A');
    for (let i = 0; i < 10; i++) await rig.render();
    rig.stall(491);
    for (let i = 0; i < 10; i++) await rig.render();
    await a.stop();
    const jumps = rig.jumps('A');
    assert.equal(jumps.length, 1); assert.ok(Math.abs(jumps[0] - 491) < 0.001, `jump ${jumps[0]}`);
    exact(placement(rig, a, 'A'), 'every chunk, before and after');
  });
});

test('D61.1: a renderer catch-up (5001, then 3365 twice) re-anchors by 3365 and never moves back', async () => {
  await device(async rig => {
    const a = await rig.start('A');
    for (let i = 0; i < 6; i++) await rig.render();
    rig.stall(3365);
    rig.dev.extra = 5001 - 3365; // the first stamp after resume briefly shows the whole stall
    for (let i = 0; i < 8; i++) await rig.render();
    await a.stop();
    const jumps = rig.jumps('A');
    assert.equal(jumps.length, 1); assert.ok(Math.abs(jumps[0] - 3365) < 0.001, `jump ${jumps[0]}`);
    exact(placement(rig, a, 'A'), 'every chunk');
  });
});

test('D61.1: a renderer catch-up burst re-anchors by what stays lost, not by stamps read during the burst', async () => {
  await device(async rig => {
    const a = await rig.start('A');
    for (let i = 0; i < 6; i++) await rig.render();
    rig.stall(2000);
    // 1830 ms of frames come back in one burst; 170 ms stay lost.
    while (rig.dev.level > 1000 + 170 + 8192 / 48) await rig.catchUp();
    rig.dev.level = 1170;
    for (let i = 0; i < 8; i++) await rig.render();
    await a.stop();
    const jumps = rig.jumps('A');
    assert.equal(jumps.length, 1); assert.ok(Math.abs(jumps[0] - 170) < 0.001, `jump ${jumps[0]}`);
    const errors = placement(rig, a, 'A').filter(error => !Number.isNaN(error));
    exact(errors, 'every chunk outside the burst');
  });
});

test('D61.1: a transient that reverts, a fall then an equal rise, and a latency change move nothing', async () => {
  // One stamp 300 ms high, then back.
  await device(async rig => {
    const a = await rig.start('A');
    for (let i = 0; i < 4; i++) await rig.render();
    rig.dev.extra = 300; await rig.render();
    for (let i = 0; i < 4; i++) await rig.render();
    await a.stop();
    assert.deepEqual(rig.jumps('A'), []);
  });
  // A confirmed fall of 300 is never applied; the later rise of 300 it offsets nets to zero.
  await device(async rig => {
    const a = await rig.start('A');
    for (let i = 0; i < 4; i++) await rig.render();
    rig.dev.level -= 300;
    for (let i = 0; i < 4; i++) await rig.render();
    rig.stall(300);
    for (let i = 0; i < 4; i++) await rig.render();
    await a.stop();
    assert.deepEqual(rig.jumps('A'), []);
  });
  // A device switch: performanceTime rises with outputLatency, the render level does not.
  await device(async rig => {
    const a = await rig.start('A');
    for (let i = 0; i < 4; i++) await rig.render();
    rig.dev.outputLatency += 0.15;
    for (let i = 0; i < 4; i++) await rig.render();
    await a.stop();
    assert.deepEqual(rig.jumps('A'), []);
  });
  // A stale stamp (same contextTime) never counts, whatever performanceTime it carries.
  await device(async rig => {
    const a = await rig.start('A');
    for (let i = 0; i < 4; i++) await rig.render();
    // Twelve batches whose readings all see the last fresh stamp's contextTime with a
    // performanceTime 2000 ms high: as fresh readings they would confirm a 2 s rise (two
    // readings, >= 100 ms apart, agreeing), so only the freshness guard keeps them out.
    const last = { ...rig.dev.stamp };
    for (let i = 0; i < 12; i++) {
      await rig.render({ stamp: () => ({ contextTime: last.contextTime, performanceTime: last.performanceTime + 2000 }) });
    }
    await a.stop();
    assert.deepEqual(rig.jumps('A'), [], 'repeated stale readings never anchor');
    exact(placement(rig, a, 'A'), 'stale stamps place nothing differently');
  });
});

test('D61.1: on a shared context a stall that A saw also places B and a later C, with the same facts', async () => {
  await device(async rig => {
    const a = await rig.start('A');
    for (let i = 0; i < 6; i++) await rig.render();
    rig.stall(1984);
    for (let i = 0; i < 6; i++) await rig.render();
    const b = await rig.start('B');
    for (let i = 0; i < 4; i++) await rig.render();
    const c = await rig.start('C');
    for (let i = 0; i < 4; i++) await rig.render();
    for (const recorder of [a, b, c]) await recorder.stop();
    exact(placement(rig, a, 'A'), 'A');
    exact(placement(rig, b, 'B'), 'B');
    exact(placement(rig, c, 'C'), 'C');
    assert.deepEqual(rig.jumps('B'), []); assert.deepEqual(rig.jumps('C'), []);
    const facts = rig.packets.filter(packet => packet.type === 'track').map(clockFields);
    assert.equal(facts.length, 3);
    for (const fact of facts) assert.deepEqual(fact, facts[0]);
  });
});

test('D61.1: B started on an idle context during a stall waits for the fresh stamp and starts right', async () => {
  await device(async rig => {
    const a = await rig.start('A');
    for (let i = 0; i < 4; i++) await rig.render();
    await a.stop();
    rig.stall(2000);                 // nothing renders; the stamp B reads on joining is stale
    const b = await rig.start('B');
    for (let i = 0; i < 6; i++) await rig.render();
    await b.stop();
    exact(placement(rig, b, 'B'), 'B from its first chunk');
    assert.deepEqual(rig.jumps('B'), []);
  });
});

test('D61.1: chunks rendered before the stall and queued past it keep their mapping', async () => {
  await device(async rig => {
    const a = await rig.start('A'), b = await rig.start('B');
    for (let i = 0; i < 4; i++) await rig.render();
    const late = node => node === a.node;
    await rig.render({ queue: late });   // A's batch, rendered before the stall, is still on its way;
    rig.stall(800);                       // B's arrival read the clock past it
    await rig.render({ queue: late });   // the first batch rendered after the stall
    await rig.flushQueued();              // A's queued batches arrive after the rise is seen
    for (let i = 0; i < 4; i++) await rig.render();
    await a.stop(); await b.stop();
    exact(placement(rig, a, 'A'), 'A: the queued pre-stall batch old, later ones new');
    exact(placement(rig, b, 'B'), 'B');
    for (const id of ['A', 'B']) {
      const jumps = rig.jumps(id);
      assert.equal(jumps.length, 1); assert.ok(Math.abs(jumps[0] - 800) < 0.001, `${id} jump ${jumps[0]}`);
    }
  });
});

test('D61.1: a freeze between a roundTrip calibration and the first stamp moves the whole span, with no jump', async () => {
  await device(async rig => {
    const a = await rig.start('A');
    assert.equal(rig.packets.find(packet => packet.type === 'track').clockMethod, 'roundTrip');
    rig.stall(1500);
    rig.dev.stamps = true;
    for (let i = 0; i < 8; i++) await rig.render();
    await a.stop();
    exact(placement(rig, a, 'A'), 'from the first chunk');
    assert.deepEqual(rig.jumps('A'), []);
  }, { stamps: false });
});

test('D61.1: a suspension re-anchors exactly once, by its wall estimate, from the suspended frame', async () => {
  await device(async rig => {
    const a = await rig.start('A');
    for (let i = 0; i < 4; i++) await rig.render();
    rig.dev.suspendMs = 700; rig.dev.state = 'suspended';
    for (const fn of [...rig.contexts[0].listeners]) fn();
    await rig.tick();
    assert.equal(rig.dev.state, 'running');
    for (let i = 0; i < 6; i++) await rig.render();
    await a.stop();
    const jumps = rig.jumps('A');
    assert.equal(jumps.length, 1); assert.ok(Math.abs(jumps[0] - 700) < 0.001, `jump ${jumps[0]}`);
    exact(placement(rig, a, 'A'), 'every chunk');
  });
});

test('D61.1: Stop during an unsettled rise sends the held chunks, placed, before trackEnd', async () => {
  await device(async rig => {
    const a = await rig.start('A');
    for (let i = 0; i < 4; i++) await rig.render();
    rig.stall(500);
    await rig.render();
    assert.equal(rig.audio('A').length, 4, 'the chunk after the rise is held');
    await a.stop();
    assert.equal(rig.audio('A').length, 5);
    const end = rig.packets.findIndex(packet => packet.type === 'trackEnd');
    assert.ok(end > rig.packets.lastIndexOf(rig.audio('A').at(-1)), 'audio before trackEnd');
    const placed = rig.audio('A');
    close(placed.at(-1).offsetMs, placed[0].offsetMs + 4 * 8192 / 48, 'held tail uses committed render continuity');
    assert.deepEqual(rig.jumps('A'), [], 'the unsettled 500 ms rise is not a span-local step');
  });
});

test('D61.1: a span that fails during an unsettled rise drops what it held', async () => {
  await device(async rig => {
    const a = await rig.start('A');
    for (let i = 0; i < 4; i++) await rig.render();
    rig.stall(500);
    await rig.render();
    rig.nodes[0].onprocessorerror();
    await a.stop();
    assert.equal(rig.audio('A').length, 4);
  }, { retries: ['retry', 'Audio capture stopped unexpectedly.'] });
});

test('D61.1: held chunks count toward the ten-second source buffer', async () => {
  await device(async rig => {
    const a = await rig.start('A');
    for (let i = 0; i < 4; i++) await rig.render();
    rig.stall(500);
    await rig.render();                  // the rise opens; this chunk is held
    const node = rig.nodes[0];
    for (let i = 0; i < 60; i++) { rig.deliver(node, { frame: node.next, truth: NaN }); node.next += 8192; }
    await rig.tick();
    assert.equal(rig.audio('A').length, 4, 'nothing is sent while the rise is unsettled');
    assert.equal(a.droppedFrames, 3 * 8192, '58 held chunks fill ten seconds');
    await rig.render();                  // the rise holds: what was held is placed and sent
    await a.stop();
    // The chunk that confirmed the rise arrived while the 58 were still pending: dropped too.
    assert.equal(a.droppedFrames, 4 * 8192);
    assert.equal(rig.audio('A').length, 4 + 58);
    const offsets = rig.audio('A').map(packet => packet.offsetMs);
    assert.ok(offsets.every((offset, i) => i === 0 || offset > offsets[i - 1]));
  }, { retries: ['gap', 'gap', 'gap', 'gap'] });
});

test('D61.1: a re-anchor of more than 60 s retires the span and the retry starts on the new anchor', async () => {
  await device(async rig => {
    const a = await rig.start('A');
    for (let i = 0; i < 4; i++) await rig.render();
    rig.stall(70000);
    await rig.render(); await rig.render();
    assert.equal(a.state, 'retrying');
    await rig.timers.pop()();          // the retry (earlier timers are the start bounds)
    for (let i = 0; i < 4; i++) await rig.render();
    await a.stop();
    const tracks = rig.packets.filter(packet => packet.type === 'track');
    assert.equal(tracks.length, 2);
    assert.deepEqual(clockFields(tracks[1]), clockFields(tracks[0]));
    const retried = { node: rig.nodes[1] };
    assert.equal(rig.audio(tracks[1].id).length, 4);
    exact(placement(rig, retried, tracks[1].id), 'the retry, on the new anchor');
  }, { retries: ['retry', 'recovered'] });
});

// R93 review: one span's Stop is no evidence about its context's clock. A rise still unsettled
// then places that span's held chunks on the committed clock; other spans keep judging it.
test('D61.1: a Stop during a one-stamp transient leaves the other spans on the context unmoved', async () => {
  await device(async rig => {
    const a = await rig.start('A'), b = await rig.start('B');
    for (let i = 0; i < 4; i++) await rig.render();
    rig.dev.extra = 300; await rig.render();          // one stamp 300 ms high, back on the next
    await a.stop();
    assert.equal(rig.audio('A').length, 5, 'A sends what it held before its end');
    exact(placement(rig, a, 'A'), 'A also stays on its committed clock');
    assert.deepEqual(rig.jumps('A'), []);
    for (let i = 0; i < 6; i++) await rig.render();
    await b.stop();
    assert.deepEqual(rig.jumps('B'), []);
    exact(placement(rig, b, 'B'), 'B after A stopped at the transient');
  });
});

for (const realStall of [false, true]) {
  test(`D61.1: a successor never overlaps a stopped span during ${realStall ? 'a later-confirmed stall' : 'a transient rise'}`, async () => {
    await device(async rig => {
      const a = await rig.start('A', { sourceTrackId: 'S' }), r = await rig.start('R');
      for (let i = 0; i < 4; i++) await rig.render();
      if (realStall) rig.stall(500); else rig.dev.extra = 60;
      await rig.render();
      assert.equal(rig.audio('A').length, 4, 'A holds the unsettled chunk');
      await a.stop();
      const lastA = rig.audio('A').at(-1), firstA = rig.audio('A')[0];
      const endA = lastA.offsetMs + lastA.frames * 1000 / 48000;
      // The committed mapping is unchanged: render-frame continuity from A's first batch.
      const committedA = firstA.offsetMs + (a.node.chunks.at(-1).frame - a.node.chunks[0].frame) / 48;
      rig.dev.extra = 0;
      const a2 = await rig.start('A2', { sourceTrackId: 'S' });
      for (let i = 0; i < 6; i++) await rig.render();
      await a2.stop(); await r.stop();
      const firstA2 = rig.audio('A2')[0];
      assert.ok(firstA2.offsetMs >= endA, `successor starts ${firstA2.offsetMs} before predecessor end ${endA}`);
      close(lastA.offsetMs, committedA, 'A stops on its committed mapping');
      assert.deepEqual(rig.packets.filter(p => p.type === 'track' && p.sourceTrackId === 'S').map(p => p.id), ['A', 'A2']);
      assert.deepEqual(rig.jumps('A'), [], 'Stop invents no span-local jump');
      assert.deepEqual(rig.jumps('A2'), [], 'the confirmed junction is before its first sample');
      if (realStall) {
        close(firstA2.offsetMs - endA, 500, 'the confirmed stall is a 500 ms gap at the successor junction');
        exact(rig.jumps('R').map(step => step - 500), 'R commits the same stall');
        assert.equal(rig.jumps('R').length, 1);
      } else {
        close(firstA2.offsetMs, endA, 'the transient leaves a contiguous successor');
        assert.deepEqual(rig.jumps('R'), [], 'a transient moves neither remote nor successor');
      }
      exact(placement(rig, a2, 'A2'), 'successor stays on the shared committed clock');
    });
  });
}

test('D61.1: a Stop on the first stamp of a renderer catch-up leaves B to confirm the real loss', async () => {
  await device(async rig => {
    const a = await rig.start('A'), b = await rig.start('B');
    for (let i = 0; i < 4; i++) await rig.render();
    rig.stall(3365); rig.dev.extra = 5001 - 3365;
    await rig.render();
    await a.stop();
    for (let i = 0; i < 6; i++) await rig.render();
    await b.stop();
    const jumps = rig.jumps('B');
    assert.equal(jumps.length, 1); assert.ok(Math.abs(jumps[0] - 3365) < 0.001, `B jump ${jumps[0]}`);
    exact(placement(rig, b, 'B'), 'B');
  });
});

test('D61.1: a rise still unsettled when the context suspends is kept, then re-anchored with the suspension', async () => {
  await device(async rig => {
    const a = await rig.start('A');
    for (let i = 0; i < 4; i++) await rig.render();
    rig.stall(800);
    await rig.render();                                // the rise opens; this chunk is held
    rig.dev.suspendMs = 700; rig.dev.state = 'suspended';
    for (const fn of [...rig.contexts[0].listeners]) fn();
    await rig.tick();
    for (let i = 0; i < 6; i++) await rig.render();
    await a.stop();
    const jumps = rig.jumps('A');
    assert.equal(jumps.length, 2, `jumps ${jumps}`);
    assert.ok(Math.abs(jumps[0] - 800) < 0.001 && Math.abs(jumps[1] - 700) < 0.001, `jumps ${jumps}`);
    exact(placement(rig, a, 'A'), 'every chunk');
  });
});

test('D61.1: losses below the step that stand out from the jitter still add up to a re-anchor (ten 45 ms stalls)', async () => {
  await device(async rig => {
    const a = await rig.start('A');
    for (let i = 0; i < 4; i++) await rig.render();
    for (let k = 0; k < 10; k++) { rig.stall(45); for (let i = 0; i < 3; i++) await rig.render(); }
    await a.stop();
    const errors = placement(rig, a, 'A');
    // Main corrected none of the 450 ms. What re-centring absorbs is bounded by STALL_CENTER.
    assert.ok(errors.every(error => error <= 0.001 && error > -STEP_BOUND), `errors ${errors.map(e => e.toFixed(1))}`);
    assert.ok(Math.abs(errors.at(-1)) < 10, `last error ${errors.at(-1)}`);
  });
});

test('D61.1: a slow fall after a re-anchor becomes credit that offsets the next rise', async () => {
  await device(async rig => {
    const a = await rig.start('A');
    for (let i = 0; i < 4; i++) await rig.render();
    rig.stall(2000);
    for (let i = 0; i < 20; i++) await rig.render({ drift: -15 });   // 300 ms caught up slowly
    const over = placement(rig, a, 'A').at(-1);
    rig.stall(300);
    for (let i = 0; i < 6; i++) await rig.render();
    await a.stop();
    const jumps = rig.jumps('A'), errors = placement(rig, a, 'A');
    assert.ok(over > 200, `the over-correction a confirmed step cannot see coming: ${over}`);
    assert.ok(jumps.every(jump => jump > 0), 'offsets never move backward');
    // The last part of the fall, under one step, is tolerated as jitter and not credited.
    assert.ok(Math.abs(errors.at(-1)) < STEP_BOUND + 1, `the next rise is offset by the credit: ${errors.at(-1)} (was ${over})`);
  });
});

test('D61.1: stamps that keep crossing the level hold chunks at most about a second, and drop none', async () => {
  let k = 0;
  await device(async rig => {
    const a = await rig.start('A');
    for (let i = 0; i < 4; i++) await rig.render();
    rig.dev.noise = () => (k++ % 2 ? -100 : 100);
    for (let i = 0; i < 80; i++) {
      await rig.render();
      // One second is six 8192-frame batches; one more arrives before the bound is checked.
      assert.ok(rig.audio('A').length >= 4 + i + 1 - 7, `held ${4 + i + 1 - rig.audio('A').length} at batch ${i}`);
    }
    rig.dev.noise = () => 0;
    await a.stop();
    assert.equal(a.droppedFrames, 0);
    assert.equal(rig.audio('A').length, 84);
    assert.deepEqual(rig.jumps('A'), [], 'noise on both sides is no step');
  });
});

// D61.1 steady drift (decision on #56, 0.2.15): a steady rate is clock drift, not lost audio. The
// audio is continuous, so offsets follow the capture clock (render frames) at any rate; only a
// discontinuity re-anchors. 12000 ppm is Apple's virtual sound device: 0.2.14 re-anchored it by
// about 121 jumps of 50 ms per ten minutes.
const TEN_MINUTES = Math.ceil(600 * 48000 / 8192);
const perBatch = ppm => 8192 / 48 * ppm / 1e6;
const report = jumps => `${jumps.length} jumps, ${jumps.reduce((sum, jump) => sum + jump, 0).toFixed(0)} ms in all`;

test('D61.1: steady drift follows the capture clock with no jump (10 min at 12000 ppm slow and fast)', async () => {
  for (const ppm of [12000, -12000]) {
    await device(async rig => {
      const a = await rig.start('A');
      for (let i = 0; i < TEN_MINUTES; i++) await rig.render({ drift: perBatch(ppm) });
      await a.stop();
      const jumps = rig.jumps('A');
      assert.equal(jumps.length, 0, `${ppm} ppm: ${report(jumps)}`);
      assert.equal(rig.audio('A').length, TEN_MINUTES);
      // Offsets are render frames on the calibration: the drift stays in the placement, bounded.
      const errors = placement(rig, a, 'A');
      close(errors.at(-1), -perBatch(ppm) * TEN_MINUTES, `${ppm} ppm: the last chunk is off by the drift alone`);
    });
  }
});

// Pulse reads stamps with about +-10 ms of jitter (pairs up to 19.6 ms apart in the #56 gate);
// +-15 and +-20 ms move a pair by more than half a step, so no fixed pair bound tells them apart.
test('D61.1: steady drift with jitter never re-anchors (10 min at 3000 to 12000 ppm, +-3 to +-20 ms)', async () => {
  let seed = 11;
  const random = () => (seed = (seed * 1103515245 + 12345) % 2147483648) / 2147483648;
  for (const ppm of [12000, -12000, 5000, 3000]) for (const jitter of [3, 12, 15, 20]) {
    await device(async rig => {
      rig.dev.noise = () => (random() * 2 - 1) * jitter;
      const a = await rig.start('A');
      for (let i = 0; i < TEN_MINUTES; i++) await rig.render({ drift: perBatch(ppm) });
      await a.stop();
      const jumps = rig.jumps('A');
      assert.equal(jumps.length, 0, `${ppm} ppm, jitter ${jitter} ms: ${report(jumps)}`);
    });
  }
});

test('D61.1: rendering steadily at 85% or 115% of real time is drift too: no jump, chunks held at most about a second', async () => {
  for (const drift of [30, -30]) {
    await device(async rig => {
      const a = await rig.start('A');
      for (let i = 0; i < 4; i++) await rig.render();
      // 30 ms per 171 ms batch, 1.8 s over the run.
      for (let i = 0; i < 60; i++) {
        await rig.render({ drift });
        assert.ok(rig.audio('A').length >= 4 + i + 1 - 7, `held ${4 + i + 1 - rig.audio('A').length} at batch ${i}`);
      }
      await a.stop();
      const jumps = rig.jumps('A');
      assert.equal(jumps.length, 0, `${drift} ms per batch: ${report(jumps)}`);
      assert.equal(rig.audio('A').length, 64);
    });
  }
});

// The line learns the rate from the readings as they come, not only once it lags a step behind.
test('D61.1: a stall early in steady drift re-anchors by the stall alone (1 s and 3 s into 12000 ppm slow and fast)', async () => {
  for (const ppm of [12000, -12000]) for (const at of [1, 3]) for (const stall of [100, 300]) {
    await device(async rig => {
      const a = await rig.start('A');
      for (let i = 0; i < Math.ceil(at * 48000 / 8192); i++) await rig.render({ drift: perBatch(ppm) });
      rig.stall(stall);
      for (let i = 0; i < 40; i++) await rig.render({ drift: perBatch(ppm) });
      await a.stop();
      const jumps = rig.jumps('A');
      assert.equal(jumps.length, 1, `${ppm} ppm, ${stall} ms stall at ${at} s: ${report(jumps)}`);
      assert.ok(Math.abs(jumps[0] - stall) < 0.01, `${ppm} ppm at ${at} s: jump ${jumps[0]}, stall ${stall}`);
    });
  }
});

// The line is a fit of jittered readings, so a stall after them is measured within a few ms.
test('D61.1: a stall after minutes of drift with jitter re-anchors by the stall alone (fast and slow, +-15 and +-20 ms)', async () => {
  let seed = 23;
  const random = () => (seed = (seed * 1103515245 + 12345) % 2147483648) / 2147483648;
  const fiveMinutes = Math.ceil(300 * 48000 / 8192);
  for (const [ppm, jitter] of [[-12000, 15], [-5000, 15], [-12000, 20], [-5000, 20], [12000, 20]]) {
    await device(async rig => {
      rig.dev.noise = () => (random() * 2 - 1) * jitter;
      const a = await rig.start('A');
      for (let i = 0; i < fiveMinutes; i++) await rig.render({ drift: perBatch(ppm) });
      rig.dev.noise = () => 0;
      for (let i = 0; i < 10; i++) await rig.render({ drift: perBatch(ppm) });
      rig.stall(300);
      for (let i = 0; i < 60; i++) await rig.render({ drift: perBatch(ppm) });
      await a.stop();
      const jumps = rig.jumps('A');
      assert.equal(jumps.length, 1, `${ppm} ppm, jitter ${jitter} ms: ${report(jumps)}`);
      assert.ok(Math.abs(jumps[0] - 300) < 10, `${ppm} ppm, jitter ${jitter} ms: jump ${jumps[0]}`);
    });
  }
});

// A rate that changes with no jump is drift too: it takes no credit that would swallow a later stall.
test('D61.1: a rate that changes with no jump is drift: a later stall re-anchors in full (12000 to -12000 ppm)', async () => {
  let seed = 31;
  const random = () => (seed = (seed * 1103515245 + 12345) % 2147483648) / 2147483648;
  const half = Math.ceil(150 * 48000 / 8192);
  for (const jitter of [0, 15, 20]) {
    await device(async rig => {
      rig.dev.noise = () => (random() * 2 - 1) * jitter;
      const a = await rig.start('A');
      for (let i = 0; i < half; i++) await rig.render({ drift: perBatch(12000) });
      for (let i = 0; i < half; i++) await rig.render({ drift: perBatch(-12000) });
      rig.dev.noise = () => 0;
      for (let i = 0; i < 10; i++) await rig.render({ drift: perBatch(-12000) });
      rig.stall(300);
      for (let i = 0; i < 60; i++) await rig.render({ drift: perBatch(-12000) });
      await a.stop();
      const jumps = rig.jumps('A');
      assert.equal(jumps.length, 1, `jitter ${jitter} ms: ${report(jumps)}`);
      assert.ok(Math.abs(jumps[0] - 300) < (jitter ? 10 : 0.01), `jitter ${jitter} ms: jump ${jumps[0]}`);
    });
  }
});

// The line follows the last FOLLOW_READINGS (about four seconds), not a fit that still holds the
// old rate: five seconds after a jitter-hidden reversal it is on the new rate, so a stall is
// measured from it and the reversal settles as no step (a 64-reading fit lagged it by 50 to 100 ms).
test('D61.1: a stall five seconds after a rate reversal under jitter re-anchors by the stall alone', async () => {
  let seed = 1;
  const random = () => (seed = (seed * 1103515245 + 12345) % 2147483648) / 2147483648;
  const batches = seconds => Math.ceil(seconds * 48000 / 8192);
  for (const [from, to, jitter] of [[12000, -12000, 12], [12000, -12000, 20], [-12000, 12000, 15], [-12000, 12000, 20]]) {
    await device(async rig => {
      rig.dev.noise = () => (random() * 2 - 1) * jitter;
      const a = await rig.start('A');
      for (let i = 0; i < batches(10); i++) await rig.render({ drift: perBatch(from) });
      for (let i = 0; i < batches(5); i++) await rig.render({ drift: perBatch(to) });
      rig.dev.noise = () => 0;
      rig.stall(300);
      for (let i = 0; i < 60; i++) await rig.render({ drift: perBatch(to) });
      await a.stop();
      const jumps = rig.jumps('A');
      assert.equal(jumps.length, 1, `${from} to ${to} ppm, jitter ${jitter} ms: ${report(jumps)}`);
      assert.ok(Math.abs(jumps[0] - 300) < 20, `${from} to ${to} ppm, jitter ${jitter} ms: jump ${jumps[0]}`);
    });
  }
});

// Credit from a fall with no stall before it offsets only a rise within STALL_RECOVER_MS (the
// fall then an equal rise above nets to zero); later, a rise is a stall of its own.
test('D61.1: a fall that is no recovery offsets only a rise within two seconds', async () => {
  await device(async rig => {
    const a = await rig.start('A');
    for (let i = 0; i < 4; i++) await rig.render();
    rig.dev.level -= 300;
    for (let i = 0; i < 18; i++) await rig.render();   // three seconds
    rig.stall(300);
    for (let i = 0; i < 6; i++) await rig.render();
    await a.stop();
    const jumps = rig.jumps('A');
    assert.equal(jumps.length, 1, report(jumps)); assert.ok(Math.abs(jumps[0] - 300) < 0.001, `jump ${jumps[0]}`);
  });
});

test('D61.1: a stall during steady drift re-anchors by the stall alone (12000 ppm slow and fast, 0.5 s and 2 s)', async () => {
  for (const ppm of [12000, -12000]) for (const stall of [500, 2000]) {
    await device(async rig => {
      const a = await rig.start('A');
      const minute = Math.ceil(60 * 48000 / 8192);
      for (let i = 0; i < minute; i++) await rig.render({ drift: perBatch(ppm) });
      rig.stall(stall);
      for (let i = 0; i < minute; i++) await rig.render({ drift: perBatch(ppm) });
      await a.stop();
      const jumps = rig.jumps('A');
      assert.equal(jumps.length, 1, `${ppm} ppm, ${stall} ms stall: ${report(jumps)}`);
      assert.ok(Math.abs(jumps[0] - stall) < 0.001, `${ppm} ppm: jump ${jumps[0]}, stall ${stall}`);
    });
  }
});

// The consumer review of #61 (d9691ea). A stutter, stalls on consecutive batches, makes a
// staircase of readings that a line fits as well as a rate: d9691ea took 4 x 200 ms (1.17) as a
// rate that changed with no jump and re-anchored none of the 800 ms. A rate of more than
// STALL_SLOPE (0.2: rendering at 85% is 0.18) is no clock rate, so it settles as steps, as in
// 0.2.14; a slower one is provisional, and readings that go back to the old rate show the
// stutter's whole loss. Chunks stay held about a second at most.
const sum = values => values.reduce((total, value) => total + value, 0);
test('D61.1: a stutter (stalls on consecutive batches) re-anchors its whole loss, at any rate', async () => {
  for (const ppm of [0, 12000, -12000]) for (const [ms, count] of [[200, 4], [100, 5], [60, 6], [30, 8], [30, 12], [100, 10]]) {
    await device(async rig => {
      const a = await rig.start('A');
      let rendered = 0;
      const render = async () => {
        await rig.render({ drift: perBatch(ppm) }); rendered++;
        assert.ok(rendered - rig.audio('A').length <= 7, `held ${rendered - rig.audio('A').length} at batch ${rendered}`);
      };
      for (let i = 0; i < 60; i++) await render();
      for (let k = 0; k < count; k++) { rig.stall(ms); await render(); }
      for (let i = 0; i < 60; i++) await render();
      await a.stop();
      const jumps = rig.jumps('A'), errors = placement(rig, a, 'A');
      assert.ok(jumps.every(jump => jump > 0), `${ppm} ppm, ${count} x ${ms} ms: ${report(jumps)}`);
      assert.ok(Math.abs(sum(jumps) - count * ms) < 1, `${ppm} ppm, ${count} x ${ms} ms: ${report(jumps)}`);
      close(errors.at(-1), -perBatch(ppm) * rendered + sum(jumps) - count * ms, `${ppm} ppm, ${count} x ${ms} ms: the last chunk is off by the drift alone`);
    });
  }
});

// d9691ea settled the first hold at its smallest reading once a reading came more than SETTLE_MS
// after it opened, and started the line's trail at a reading that already held the second stall,
// so that stall became the baseline: 150, then 1000 ms one batch later, jumped 150 and lost 1000.
// A latest reading beyond the settled step by more than STALL_AGREE_MS opens a new hold at once.
test('D61.1: a second stall one batch after another re-anchors both (150 then 1000 ms, three 400 ms)', async () => {
  for (const ppm of [0, 12000, -12000]) for (const stalls of [[150, 1000], [60, 830], [150, 2000], [500, 1000], [400, 400, 400]]) {
    await device(async rig => {
      const a = await rig.start('A');
      for (let i = 0; i < 60; i++) await rig.render({ drift: perBatch(ppm) });
      for (const ms of stalls) { rig.stall(ms); await rig.render({ drift: perBatch(ppm) }); }
      for (let i = 0; i < 60; i++) await rig.render({ drift: perBatch(ppm) });
      await a.stop();
      const jumps = rig.jumps('A');
      assert.ok(jumps.every(jump => jump > 0), `${ppm} ppm, ${stalls.join(' + ')} ms: ${report(jumps)}`);
      assert.ok(Math.abs(sum(jumps) - sum(stalls)) < 0.01, `${ppm} ppm, ${stalls.join(' + ')} ms: ${report(jumps)}`);
    });
  }
});

// Both, under jitter: every session re-anchors the loss within a step.
test('D61.1: stutters and stalls one batch apart under +-15 ms jitter re-anchor their loss within 50 ms', async () => {
  const cases = { '4 x 200': [200, 200, 200, 200], '5 x 100': Array(5).fill(100), '8 x 30': Array(8).fill(30), '12 x 30': Array(12).fill(30), '150 + 1000': [150, 1000], '3 x 400': [400, 400, 400] };
  for (const [name, stalls] of Object.entries(cases)) for (let seed = 0; seed < 10; seed++) {
    let s = 9000 + seed;
    const random = () => (s = (s * 1103515245 + 12345) % 2147483648) / 2147483648;
    await device(async rig => {
      rig.dev.noise = () => (random() * 2 - 1) * 15;
      const a = await rig.start('A');
      for (let i = 0; i < 60; i++) await rig.render();
      for (const ms of stalls) { rig.stall(ms); await rig.render(); }
      for (let i = 0; i < 60; i++) await rig.render();
      await a.stop();
      const jumps = rig.jumps('A');
      assert.ok(Math.abs(sum(jumps) - sum(stalls)) <= STEP_BOUND, `${name} ms, seed ${seed}: ${report(jumps)}`);
    });
  }
});

// Minors 2 and 5 of that review are known limits (SPEC D61.1); these bound them, on fixed
// seeds, so they do not grow. At 85% under +-15 and +-20 ms jitter d9691ea jumped in 61 of 200 sessions while the
// line learned the rate (the slope bound and the junction now allow for the jitter): at
// most 25. A renderer catch-up one second into a session under the same jitter: d9691ea
// mis-sized 33 of 100 by more than a step, extrapolating a slope fitted to six jittered readings
// over the caught-up frames (the line now takes a fitted slope only once its standard error is
// within FOLLOW_SE): at most 15.
test('D61.1: known limits under +-15 and +-20 ms jitter stay bounded (85% rendering, early catch-up)', async () => {
  let jumped = 0, missized = 0;
  for (const jitter of [15, 20]) for (let seed = 0; seed < 100; seed++) {
    let s = 5000 + seed;
    const random = () => (s = (s * 1103515245 + 12345) % 2147483648) / 2147483648;
    await device(async rig => {
      rig.dev.noise = () => (random() * 2 - 1) * jitter;
      const a = await rig.start('A');
      for (let i = 0; i < 176; i++) await rig.render({ drift: 30 });
      await a.stop();
      if (rig.jumps('A').length) jumped++;
    });
  }
  for (const jitter of [15, 20]) for (let seed = 0; seed < 50; seed++) {
    let s = 6000 + seed;
    const random = () => (s = (s * 1103515245 + 12345) % 2147483648) / 2147483648;
    await device(async rig => {
      rig.dev.noise = () => (random() * 2 - 1) * jitter;
      const a = await rig.start('A');
      for (let i = 0; i < 6; i++) await rig.render();
      const base = rig.dev.level;
      rig.stall(2000);
      while (rig.dev.level > base + 170 + 8192 / 48) await rig.catchUp();
      rig.dev.level = base + 170;
      for (let i = 0; i < 100; i++) await rig.render();
      await a.stop();
      if (Math.abs(sum(rig.jumps('A')) - 170) > STEP_BOUND) missized++;
    });
  }
  assert.ok(jumped <= 25, `85% rendering: ${jumped} of 200 sessions jumped`);
  assert.ok(missized <= 15, `early catch-up: ${missized} of 100 mis-sized by more than a step`);
});

// The second review of #61 (ec2343c). A train of stalls, each above the step and two or three
// batches apart, makes a staircase whose readings scatter about their own line far more than the
// jitter does. ec2343c widened the junction and the slope bound by that scatter, so a train of
// more than about three seconds passed as a rate change and was lost (10 x 100 ms every two
// batches: 100 of 1000 ms re-anchored); d9691ea, with the junction at STALL_AGREE_MS, re-anchored
// every one. The scatter allowed is now the jitter the in-band readings showed before the hold,
// and held readings that scatter about their line by more than twice it are no rate.
test('D61.1: a train of stalls two or three batches apart re-anchors each, however long it lasts', async () => {
  const cases = [[0, 100, 2, 10], [0, 100, 2, 20], [0, 60, 2, 10], [0, 80, 2, 20], [10, 100, 2, 10], [10, 100, 3, 6], [10, 100, 3, 20], [10, 80, 2, 20]];
  for (const ppm of [0, 12000, -12000]) for (const [jitter, ms, every, count] of cases) for (let seed = 0; seed < (jitter ? 3 : 1); seed++) {
    let s = 7000 + seed * 101 + ms + every * 7 + count;
    const random = () => (s = (s * 1103515245 + 12345) % 2147483648) / 2147483648;
    await device(async rig => {
      rig.dev.noise = () => (random() * 2 - 1) * jitter;
      const a = await rig.start('A');
      for (let i = 0; i < 60; i++) await rig.render({ drift: perBatch(ppm) });
      for (let k = 0; k < count; k++) { rig.stall(ms); for (let i = 0; i < every; i++) await rig.render({ drift: perBatch(ppm) }); }
      for (let i = 0; i < 80; i++) await rig.render({ drift: perBatch(ppm) });
      await a.stop();
      const jumps = rig.jumps('A'), name = `${ppm} ppm, +-${jitter} ms, ${count} x ${ms} ms every ${every} batches, seed ${seed}`;
      assert.ok(jumps.every(jump => jump > 0), `${name}: ${report(jumps)}`);
      assert.ok(Math.abs(sum(jumps) - count * ms) <= (jitter ? STEP_BOUND : 1), `${name}: ${report(jumps)}`);
    });
  }
});
