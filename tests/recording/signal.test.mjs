import assert from 'node:assert/strict';
import {test} from 'node:test';
import {energy} from './signal.mjs';
import {savedCapture} from './source-changes.mjs';

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

test('lifecycle waits for a terminal event and accepts an explicitly recorded gap warning', () => {
  const capture = {manifest: {state: 'recording', events: []}, tracks: [{kind: 'microphone', frames: 48000}]};
  assert.equal(savedCapture(capture, 'callEnded'), false);
  capture.manifest = {state: 'completed', events: [{type: 'end', reason: 'callEnded'}]};
  capture.tracks[0].ended = true;
  assert.equal(savedCapture(capture, 'callEnded'), capture);
  capture.manifest.state = 'interrupted'; capture.manifest.problemKind = 'gap';
  capture.tracks[0].gapFrames = 48000;
  assert.equal(savedCapture(capture, 'callEnded'), capture);
});

test('lifecycle refuses IO, failed source, wrong end cause, unfinished tracks and unreported gaps', () => {
  const valid = {manifest: {state: 'interrupted', problemKind: 'gap', events: [{type: 'end', reason: 'callEnded'}]},
    tracks: [{kind: 'microphone', frames: 48000, ended: true, gapFrames: 48000}]};
  for (const change of [capture => {capture.manifest.problemKind = 'io';},
    capture => {capture.manifest.problemKind = 'capture';},
    capture => {capture.manifest.events[0].reason = 'error';},
    capture => {capture.tracks[0].ended = false;},
    capture => {delete capture.tracks[0].gapFrames;}]) {
    const capture = structuredClone(valid); change(capture);
    assert.throws(() => savedCapture(capture, 'callEnded'), assert.AssertionError);
  }
});
