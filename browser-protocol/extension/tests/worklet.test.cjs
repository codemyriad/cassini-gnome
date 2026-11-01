const { test } = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');

function processor(channels, armed = true) {
  const packets = [];
  const context = vm.createContext({ currentFrame: 96000, AudioWorkletProcessor: class {
    constructor() { this.port = { postMessage: message => packets.push(message) }; }
  }, registerProcessor: (_, type) => { context.type = type; } });
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../pcm-worklet.js'), 'utf8'), context);
  const node = new context.type({ processorOptions: { channels } });
  if (armed) node.port.onmessage({data:"start"});
  return {node, packets, context};
}

test('worklet batches interleaved PCM, clips samples and retains render timestamps', () => {
  const { node, packets } = processor(2);
  node.process([[new Float32Array(8192).fill(2), new Float32Array(8192).fill(-2)]]);
  assert.equal(packets.length, 1);
  assert.equal(packets[0].frame, 96000);
  const pcm = new Int16Array(packets[0].pcm);
  assert.equal(pcm.length, 16384);
  assert.deepEqual([...pcm.slice(0, 4)], [32767, -32767, 32767, -32767]);
});
test('stop flushes a partial block before acknowledging completion', () => {
  const { node, packets } = processor(1);
  node.process([[new Float32Array(128).fill(0.5)]]);
  assert.equal(packets.length, 0);
  node.port.onmessage({ data: 'stop' });
  assert.equal(packets[0].pcm.byteLength, 256);
  assert.equal(packets[0].frame, 96000);
  assert.equal(packets[1].stopped, true);
  assert.equal(node.process([[]]), false);
});
test('missing and non-finite samples become silence', () => {
  const { node, packets } = processor(2);
  node.process([[new Float32Array(128).fill(NaN)]]);
  node.port.onmessage({ data: 'stop' });
  assert.ok([...new Int16Array(packets[0].pcm)].every(sample => sample === 0));
});


test('worklet readiness discards initialization frames and preserves the first admitted render timestamp', () => {
  const {node,packets,context}=processor(1,false);
  context.currentFrame=0;
  node.process([[new Float32Array(128).fill(.75)]]);
  assert.equal(packets.length,0);
  context.currentFrame=1920;
  node.process([[new Float32Array(128).fill(.75)]]);
  assert.deepEqual(JSON.parse(JSON.stringify(packets)),[{ready:true}]);
  context.currentFrame=2048;
  node.process([[new Float32Array(128).fill(.75)]]);
  assert.equal(packets.length,1,'no PCM before the declaration ACK arms capture');
  node.port.onmessage({data:'start'});
  context.currentFrame=2176;
  node.process([[new Float32Array(128).fill(.25)]]);
  node.port.onmessage({data:'stop'});
  assert.equal(packets[1].frame,2176);
  assert.equal(packets[1].pcm.byteLength,256);
  assert.ok([...new Int16Array(packets[1].pcm)].every(value=>value===8192));
});
