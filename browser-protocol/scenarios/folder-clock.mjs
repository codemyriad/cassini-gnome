// The folder clock: how far a live recording folder has got, read from its session.json.
// Shared by both app adapters (shared test 2, D68). No dependencies, no product paths.
//
// S is the folder clock: the latest track end, startOffsetMs + frames * 1000 / sampleRate,
// over every track session.json lists. F(source) is the same over one source's tracks.
// Writers checkpoint session.json about once a second, so both are lower bounds of what
// the app has written; waits compare against them, never against elapsed time (E1).
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import { monotonic } from './session-clock.mjs';

export const POLL_MS = 100;

const pause = ms => new Promise(resolve => setTimeout(resolve, ms));

// [start, end) in session milliseconds, or null while the track's start or length is unknown.
export function extent(track) {
  if (!track || !Number.isFinite(track.startOffsetMs) || !Number.isInteger(track.frames) ||
      !Number.isInteger(track.sampleRate) || track.sampleRate <= 0) return null;
  return [track.startOffsetMs, track.startOffsetMs + track.frames * 1000 / track.sampleRate];
}

function selector(source) {
  if (typeof source === 'function') return source;
  if (typeof source === 'string') return track => track.sourceTrackId === source;
  if (source && typeof source === 'object') {
    const entries = Object.entries(source);
    return track => entries.every(([name, value]) => track[name] === value);
  }
  throw new TypeError('a source is a sourceTrackId, a {member: value} filter or a predicate');
}

export function tracksOf(manifest, source) {
  const tracks = Array.isArray(manifest?.tracks) ? manifest.tracks.filter(t => t && typeof t === 'object') : [];
  return source === undefined ? tracks : tracks.filter(selector(source));
}

// -Infinity while no matching track has a known extent, so "F(x) >= t" is simply false.
export function F(manifest, source) {
  return Math.max(...tracksOf(manifest, source).map(extent).filter(Boolean).map(([, end]) => end));
}

export function S(manifest) {
  return F(manifest);
}

export function eventsOf(manifest, type) {
  const events = Array.isArray(manifest?.events) ? manifest.events.filter(e => e && typeof e === 'object') : [];
  return type === undefined ? events : events.filter(e => e.type === type);
}

// A tolerant poller for one folder's session.json. A read that finds no file yet, or a
// partial or invalid document (a writer that does not replace atomically), keeps the last
// good manifest; reads and rejected reads are counted for the failure message.
export function folderClock(directory, { pollMs = POLL_MS } = {}) {
  const file = path.join(directory, 'session.json');
  const clock = {
    directory, manifest: null, reads: 0, rejected: 0, lastProblem: null,
    async read() {
      clock.reads++;
      let text;
      try {
        text = await readFile(file, 'utf8');
      } catch (error) {
        if (error.code !== 'ENOENT') throw error;
        clock.rejected++; clock.lastProblem = 'no session.json yet';
        return clock.manifest;
      }
      try {
        const value = JSON.parse(text);
        if (!value || typeof value !== 'object' || !Array.isArray(value.tracks)) throw new SyntaxError('no tracks array');
        clock.manifest = value;
      } catch (error) {
        if (!(error instanceof SyntaxError)) throw error;
        clock.rejected++; clock.lastProblem = `unreadable session.json: ${error.message}`;
      }
      return clock.manifest;
    },
    S: () => S(clock.manifest),
    F: source => F(clock.manifest, source),
    // Polls until pred(manifest) returns a truthy value and returns it. pred may throw to
    // fail at once (an invariant broken while waiting). boundMs only bounds the wait: it
    // fails with the label and the last folder state, never returns quietly.
    async waitFolder(pred, label, boundMs) {
      if (!Number.isFinite(boundMs) || boundMs <= 0) throw new RangeError(`${label}: bound must be positive`);
      const end = monotonic() + boundMs;
      for (;;) {
        const manifest = await clock.read();
        if (manifest) {
          const value = await pred(manifest);
          if (value) return value;
        }
        if (monotonic() >= end) throw new Error(`Timed out after ${boundMs} ms: ${label} (${clock.describe()})`);
        await pause(pollMs);
      }
    },
    describe() {
      const tracks = tracksOf(clock.manifest).map(t => ({ id: t.id, kind: t.kind, sourceTrackId: t.sourceTrackId,
        extent: extent(t), ended: t.ended }));
      return JSON.stringify({ S: clock.S(), state: clock.manifest?.state, tracks,
        events: eventsOf(clock.manifest).slice(-6), reads: clock.reads, rejected: clock.rejected, lastProblem: clock.lastProblem });
    },
  };
  return clock;
}

// A bounded poll of anything that is not the folder (an app status, a page, a pid file).
export async function poll(check, label, boundMs, { pollMs = POLL_MS } = {}) {
  const end = monotonic() + boundMs;
  let last;
  for (;;) {
    last = await check();
    if (last) return last;
    if (monotonic() >= end) throw new Error(`Timed out after ${boundMs} ms: ${label}`);
    await pause(pollMs);
  }
}
