// The driver's session clock (D68 shared test 2). SPEC: the session origin is a wall-clock
// Date.now(); a capturing document maps it onto its own monotonic clock once and never reads the
// wall clock again. The driver does the same, at the origin's own instant, so its requested and
// observed times share the apps' monotonic timeline: a wall clock that drifts or steps during a
// run (a hosted VM's ran 500 ppm fast) can no longer move them.
//
// monotonic() is the system's monotonic clock in milliseconds (process.hrtime:
// CLOCK_MONOTONIC on Linux, mach_absolute_time on macOS). Unlike performance.now(), which each
// process counts from its own start, it is one clock for every process on the machine, so the
// control relay's frame stamps and the driver's readings compare directly.
export const monotonic = () => Number(process.hrtime.bigint()) / 1e6;

// One reading of where the wall clock stood at a monotonic instant: the wall reading between two
// monotonic ones, and how far from their midpoint it can be (a descheduled reader widens it).
export function wallPair({ wall = Date.now, mono = monotonic } = {}) {
  const before = mono(), atWall = wall(), after = mono();
  return { mono: (before + after) / 2, wall: atWall, spread: (after - before) / 2 };
}

// Between two readings in a row the wall clock may slew, never step: its change matches the
// monotonic one within a whole-millisecond reading, the readings' own spreads and WALL_SLEW (NTP
// slews 500 ppm, and up to 5000 ppm on an offset over a second; a hosted VM's ran 510 ppm).
// Anything more is a step, and no mapping crosses it. The driver reads the wall clock every
// ORIGIN_SAMPLE_MS while Record runs, so the two readings around the origin are that close.
export const WALL_READ_MS = 1, WALL_SLEW = 5e-3;
const spread = reading => reading.spread ?? 0;
const continuous = (a, b) => Math.abs((b.wall - a.wall) - (b.mono - a.mono)) <= WALL_READ_MS + spread(a) + spread(b) + WALL_SLEW * (b.mono - a.mono);

// Maps a wall-clock origin (Date.now() ms) onto the monotonic clock once, at the origin's own
// instant. An app maps it when its recording starts; the driver learns it later (Mac #149: about
// 2.35 s), and a wall clock slewing 510 ppm on a hosted VM moved a mapping read then by 1.2 ms
// against the app's, enough to put a selection stamped at once before its request. So `pairs`
// are wallPair readings the caller took while the origin was being taken: the first before
// Record, the last after the extension reported it. One more is read now.
//
// The readings split into stretches where the wall clock only slewed. Within a stretch the origin
// is placed linearly between the two readings in a row that bracket it, or from the stretch's
// nearer end, each with its uncertainty (the readings' spreads, and the slew over any
// extrapolation). A placement counts only if it falls, within that uncertainty, after the
// stretch before it ends and before the stretch after it starts, and never before the first pair
// or after the reading now: the origin was taken in between. Placements that agree within their
// uncertainties are one answer; none, or two that disagree (a step near the origin that both
// sides of it could have read), throws rather than guess.
// at(m) is the session time of a monotonic reading m; now() reads it.
export function sessionClock(originMs, { pairs = [], wall = Date.now, mono = monotonic } = {}) {
  const readings = [...pairs, wallPair({ wall, mono })].sort((a, b) => a.mono - b.mono);
  const stretches = [[readings[0]]];
  for (const reading of readings.slice(1)) {
    if (continuous(stretches.at(-1).at(-1), reading)) stretches.at(-1).push(reading);
    else stretches.push([reading]);
  }
  const edge = reading => WALL_READ_MS + spread(reading);
  const first = readings[0], last = readings.at(-1);
  const placed = [];
  stretches.forEach((stretch, i) => {
    const previous = stretches[i - 1]?.at(-1) ?? (pairs.length ? first : null), next = stretches[i + 1]?.[0] ?? last;
    const from = previous ? previous.mono - edge(previous) : -Infinity, to = next.mono + edge(next);
    const { at, within } = place(stretch, originMs);
    if (at + within >= from && at - within <= to) placed.push({ at, within });
  });
  if (!placed.length) throw new Error(`session clock: the origin ${originMs} fits no stretch of the wall clock read since before Record`);
  const ats = placed.map(p => p.at);
  if (Math.max(...ats) - Math.min(...ats) > placed.reduce((sum, p) => sum + p.within, 0)) {
    throw new Error(`session clock: the wall clock stepped near the origin ${originMs}, which fits monotonic ${ats.map(m => m.toFixed(1)).join(' and ')}`);
  }
  const originMono = placed[0].at;
  return { originMono, at: m => m - originMono, now: () => mono() - originMono };
}

// One stretch's placement and its uncertainty: between the two readings in a row that bracket the
// origin, else from the reading nearest it in wall time, plus the slew over that extrapolation.
function place(stretch, originMs) {
  for (let i = 1; i < stretch.length; i++) {
    const a = stretch[i - 1], b = stretch[i];
    if (a.wall <= originMs && originMs <= b.wall && b.wall > a.wall) {
      return { at: a.mono + (originMs - a.wall) * (b.mono - a.mono) / (b.wall - a.wall), within: WALL_READ_MS + Math.max(spread(a), spread(b)) };
    }
  }
  const near = stretch.reduce((best, r) => (Math.abs(r.wall - originMs) < Math.abs(best.wall - originMs) ? r : best));
  return { at: near.mono - (near.wall - originMs), within: WALL_READ_MS + spread(near) + WALL_SLEW * Math.abs(near.wall - originMs) };
}
