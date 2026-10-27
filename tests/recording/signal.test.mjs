import assert from 'node:assert/strict';
import {test} from 'node:test';
import {energy} from './signal.mjs';

function tone(jump = 0) {
  const bytes = Buffer.alloc(48000 * 2 * 2);
  for (let n = 0; n < 48000 * 2; n++) {
    const phase = n >= 0.85 * 48000 ? jump : 0;
    bytes.writeInt16LE(Math.round(0.2 * 32768 * Math.sin(2 * Math.PI * 440 * n / 48000 + phase)), n * 2);
  }
  return {rate: 48000, channels: 1, bytes};
}

test('tone amplitude survives a graph phase correction without weakening bleed limits', () => {
  for (const jump of [0, 2.513, Math.PI]) {
    const source = tone(jump);
    // Only the 100 ms window containing the jump can cancel completely.
    assert.ok(energy(source, .5, 1.5, 440).tone >= 0.2 * 0.9 - 0.0001);
    assert.ok(energy(source, .5, 1.5, 880).tone < .01);
    assert.ok(energy(source, .5, 1.5, 440).rms > .14);
  }
});

test('silence and exact measurement bounds stay silent', () => {
  const source = tone();
  source.bytes.fill(0, 48000 * 2, 48000 * 2 * 2);
  assert.deepEqual(energy(source, 1, 2, 440), {rms: 0, tone: 0});
  assert.ok(energy(source, .5, .6, 440).tone > .19);
});
