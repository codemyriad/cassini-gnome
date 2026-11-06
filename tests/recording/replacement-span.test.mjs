// SPDX-License-Identifier: GPL-3.0-or-later
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {replacementSpan} from './replacement-span.mjs';
const span = (id, seconds = 2, extra = {}) => ({id, sourceTrackId:'sender', kind:'local', channels:1,
  sampleRate:8000, frames:seconds*8000, audio:{bytes:new Uint8Array(seconds*16000)}, ...extra});

test('replacement is identified by id even when the manifest order changes', () => {
  const original=span('original'), replacement=span('replacement');
  assert.equal(replacementSpan([replacement, original], original, 1.8), replacement);
  assert.equal(replacementSpan([original, span('short',1), replacement], original, 1.8), undefined,
    'a later healthy span cannot replace the first successor');
});

test('a refused replacement fails immediately even after a healthy retry', () => {
  const original=span('original'), refused=span('refused',0,{ended:true,problemKind:'capture',
    startOffsetMs:7166.0167,endOffsetMs:7166.0167,gaps:[]});
  assert.throws(()=>replacementSpan([original,refused,span('retry')],original,1.8), error =>
    /replacement span refused/.test(error.message) && /refused/.test(error.message) && /7166.0167/.test(error.message));
});

test('an ended zero-frame span fails without requiring a problem field', () => {
  const original=span('original');
  assert.throws(()=>replacementSpan([original,span('zero',0,{ended:true})],original,1.8), /replacement span refused/);
});
