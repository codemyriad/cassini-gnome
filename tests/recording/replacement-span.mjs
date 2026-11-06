// SPDX-License-Identifier: GPL-3.0-or-later
// The first new span is the replacement. A later retry must not hide its refusal.
export function replacementSpan(tracks, initial, seconds) {
  const local = tracks.filter(track => track.kind === 'local');
  if (local.some(track => track.sourceTrackId === initial.sourceTrackId &&
      (track.problemKind || (track.ended && track.frames === 0)))) {
    const facts = local.map(({id, startOffsetMs, endOffsetMs, frames, problemKind, gaps}) =>
      ({id, startOffsetMs, endOffsetMs, frames, problemKind, gaps}));
    throw new Error(`replacement span refused: ${JSON.stringify(facts)}`);
  }
  const after = local.filter(track => track.id !== initial.id), replacement = after[0];
  return replacement && replacement.audio.bytes.length / (2 * replacement.channels * replacement.sampleRate) >= seconds
    ? replacement : undefined;
}
