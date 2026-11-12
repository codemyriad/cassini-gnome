export function base64(buffer) {
  const bytes = new Uint8Array(buffer);
  let text = "";
  // Convert bounded blocks: per-byte concatenation creates a string node for every
  // PCM byte. Keep each call below engine argument limits, including large buffers.
  for (let i = 0; i < bytes.length; i += 8192) {
    text += String.fromCharCode.apply(null, bytes.subarray(i, i + 8192));
  }
  return btoa(text);
}

async function bounded(promise, message, schedule = setTimeout, cancel = clearTimeout) {
  let timer;
  try {
    return await Promise.race([promise, new Promise((_, reject) => { timer = schedule(() => reject(new Error(message)), 4000); })]);
  } finally { cancel(timer); }
}

// D68.1: Chromium moves an AudioContext whose output has been all zeros for about 30 s (its
// silent sink suspender) off the audio device onto a timer, which on some machines delivers far
// less than real time (0.3-0.5 on some CI virtual machines). The capture graph's output is silent by design,
// and the worklet stamps every frame by its context's render clock, so every track would fall
// behind the session. An inaudible constant (6e-8, -144 dBFS, below one 16-bit step) into the
// destination keeps the context on the device clock. Once per context, shared ones included;
// it never reaches the PCM (the worklet reads only its source).
export const KEEP_REAL_TIME_LEVEL = 6e-8;
const keepingRealTime = new WeakMap();
export function keepRealTime(context) {
  if (keepingRealTime.has(context)) return keepingRealTime.get(context);
  const constant = context.createConstantSource();
  constant.offset.value = KEEP_REAL_TIME_LEVEL;
  constant.connect(context.destination);
  constant.start();
  keepingRealTime.set(context, constant);
  return constant;
}

// D58 (R79): a classified start failure. `resultCode` is the open control-result
// vocabulary (SPEC-CONTROL "Result codes"), never the audio-plane `code`.
const classified = resultCode => error => {
  throw Object.assign(error instanceof Error ? error : new Error(String(error)), { resultCode });
};
// D61: one calibration per AudioContext, shared by every span on it. A pending
// calibration is its promise; a finished one is the context's clock record: the clock facts
// and clockOffset, fixed once measured, plus frame-indexed anchors that only ever add time
// (a resumed suspension, a render stall) and the stall detector's state (D61.1).
const clocks = new WeakMap();
const retirementReason = error => error?.code === "callChanged" ? "callChanged" :
  error?.code === "state" && error.scope === "source" && error.retired === true ? "ended" :
  error?.code === "state" && ["source", "command"].includes(error.scope) ? "stopped" : null;
const resuming = new WeakMap();
function resumeSuspended(context) {
  if (resuming.has(context)) return resuming.get(context);
  const began = performance.now(), render = context.currentTime || 0;
  const task = bounded(Promise.resolve().then(() => context.resume()), "Audio capture did not resume within four seconds.").then(() => {
    if (context.state !== "running") throw new Error("Audio capture remains suspended.");
    const gap = Math.max(0, performance.now() - began - ((context.currentTime || 0) - render) * 1000);
    const clock = clocks.get(context);
    if (!clock?.anchors) return;
    // Frames rendered before the suspension keep their mapping; the wall estimate applies
    // from the frame it began at. The detector then reseeds, so it never counts it twice,
    // unless a rise is still open.
    if (gap > 0) { clock.anchors.push({ fromFrame: Math.round(render * clock.rate), addMs: gap }); clock.recovered = performance.now(); }
    if (clock.pending && clock.level !== null) {
      // A rise still unsettled when the context suspended is not dropped: the line and the
      // readings move by the wall estimate, so what the stall lost stays a deviation to judge.
      clock.level += gap;
      for (const reading of [...clock.pending.readings, ...clock.trail]) reading.x += gap;
      clock.pending.provisional = null;
    } else { clock.level = null; clock.ref = null; clock.pending = null; clock.trail = []; }
    clock.provisional = null;
    observe(context, clock);
  });
  resuming.set(context, task);
  task.then(() => resuming.delete(context), () => resuming.delete(context));
  return task;
}

// D61.1 (extension 0.2.14): a render stall. While a context's rendering stalls its frames are
// never rendered, so render frames alone would place every later chunk early by the stall, and
// a later span on the context would inherit that. The audio thread writes each
// getOutputTimestamp pair, so its performanceTime - contextTime - outputLatency (the render
// instant minus the render position) is a level that main-thread jank cannot move; it rises by
// exactly the rendered time a stall lost. The detector tracks that level as a line: the level at
// the last reading plus a slope per rendered millisecond.
// Steady drift (0.2.15, the decision on #56): a steady rate is clock drift, not lost audio. The
// audio is continuous, so offsets follow the capture clock (render frames) at any rate, and only
// a discontinuity re-anchors. While the in-band readings (within STALL_STEP_MS of the line) are
// steady, the line is the least-squares line of the last FOLLOW_READINGS of them (about four
// seconds), from SETTLE_READINGS + 1 on, so it learns the rate as the readings come, follows a
// rate that changes within a few seconds, and steady drift stays in the band; otherwise the line
// keeps its slope and is re-centred no faster than STALL_CENTER (2 ms per rendered second). The
// line takes a fitted slope only once its standard error is within FOLLOW_SE (20 ms over two
// seconds); until then it keeps the slope it has (none at first) and is re-centred on the
// readings, as a slope fitted to a few jittered readings would be extrapolated over every frame a
// renderer catch-up returns (the #61 review).
// Steadiness is judged over the whole trail (up to TRAIL_READINGS readings), so a discontinuity
// keeps the line from following until it leaves the trail. Readings are steady when no
// consecutive pair's move off the slope stands out from the others': farther from their median
// move than half a step or STALL_SPREAD times the median distance from it, whichever is larger.
// A discontinuity stands out, while jitter of any size moves every pair alike. So a loss is seen
// only above the jitter: losses that form a steady rate, or that the jitter hides, are drift;
// small stalls that stand out (45 ms ones under 3 ms of jitter) still add up to a step. An
// in-band reading that stands out from the last FOLLOW_READINGS readings' distances to the line
// is suspect: the line does not follow it, and a hold that opens next starts at it. The line
// does not follow within STALL_RECOVER_MS of a re-anchor. A reading beyond STALL_STEP_MS from
// the line opens a hold, which is then told apart:
// - drift: the excursion continues the trend of the readings before it (their least-squares line,
//   over at least SETTLE_READINGS of up to TRAIL_READINGS readings, extended over the held ones,
//   agrees with them within STALL_AGREE_MS on average), or, once at least SETTLE_READINGS readings
//   are held, the held readings' own line meets the last reading before them within
//   STALL_AGREE_MS plus twice the jitter (a rate that changed with no jump, such as rendering at
//   85% from some point on); and the readings of that line are steady, and its slope is a rate:
//   at most STALL_SLOPE (rendering at 85% is 0.18) beyond twice the standard error the jitter
//   gives it. The jitter is the scatter of the steady trail about its own line, once
//   NOISE_READINGS long (until then the held readings' own scatter); held readings that scatter
//   about their line by more than twice it and STALL_AGREE_MS / 2 are no rate but a staircase,
//   a train of stalls a few batches apart, and settle as steps.
//   The line takes that slope, nothing re-anchors and no credit is taken.
// - a stutter: stalls on consecutive batches make a staircase that a line fits as well as a
//   rate. One steeper than STALL_SLOPE is no clock rate and settles as steps (below). A rate that
//   changed at the junction by more than STALL_STUTTER is provisional: a hold that opens within
//   STALL_RECOVER_MS of rendering after it, whose readings' own line keeps nearer to the slope
//   before the change than to the changed one, is the rendering going back to its rate at once,
//   so the change was a stutter. Its readings' mean distance from the line before the change,
//   when more than a step, is what the stutter lost: the context re-anchors by it and the line
//   goes back to that slope. Such a hold waits for SETTLE_READINGS readings to tell.
// - a step: anything else. A rise beyond STALL_STEP_MS that holds on a later fresh stamp, read
//   at least STALL_CONFIRM_MS later (agreeing within STALL_AGREE_MS), re-anchors the context
//   forward by the smaller agreeing step from the line, from the stalled frame on. While fewer
//   than SETTLE_READINGS readings are held, an agreeing pair settles only a discontinuity at the
//   junction: the pair sits more than a step beyond the last SETTLE_READINGS in-band readings
//   (from the line). A rate that changed with no jump creeps up to the band, so its last in-band
//   readings sit near the held ones and the hold runs on until its own line can tell. When the
//   latest reading sits beyond the step taken by more than STALL_AGREE_MS (a second stall within
//   the hold, or a stutter going on), it opens a new hold at once, judged from the reading the step
//   was taken at, and the held chunks rendered before it go.
// The wall separation matters after a renderer stall: the page then reads a backlog of chunks
// within a millisecond while rendering catches up faster than real time, so their stamps fall
// steadily and neighbours agree by chance. A rise that neither confirms nor reverts settles at
// its smallest reading SETTLE_MS after it opened; one that keeps flipping sign settles at no
// step then, so no chunk is held longer. At a span's Stop an unsettled rise places that
// span's held chunks on the committed mapping; the rise stays open for the context's other
// and later spans. A rise seen first against the calibration seed (plus the roundTrip uncertainty) is a
// freeze between calibration and the first device callbacks. Offsets never move backward: a
// confirmed fall that is no drift is credit that offsets a later rise. A fall that opens within
// STALL_RECOVER_MS of a re-anchor or of such a fall, ramp or not, is rendering returning part of
// what a stall lost, not a rate: its credit waits for the next rise. Any other fall's credit
// offsets only a rise that settles within STALL_RECOVER_MS of it. The origin is never re-read
// and the clock facts never change.
// The lost time reaches the host as an offset jump, its `jump` gap.
export const STALL_STEP_MS = 50, STALL_AGREE_MS = 20, STALL_CONFIRM_MS = 100, STALL_CENTER = 0.002, STALL_SPREAD = 6, STALL_SLOPE = 0.2, STALL_STUTTER = 0.05;
const SETTLE_READINGS = 4, SETTLE_MS = 1000, TRAIL_READINGS = 64, FOLLOW_READINGS = 24, STALL_RECOVER_MS = 2000, FOLLOW_SE = 0.01, NOISE_READINGS = 24;
function clockRecord({ level0, seedSlack, seedTime, stampTime, ...measured }) {
  return { ...measured, anchors: [{ fromFrame: 0, addMs: 0 }], level: level0, slope: 0, seedSlack, seeded: false,
    lastStampTime: stampTime, ref: { contextTime: seedTime }, trail: [], noise: null, pending: null, provisional: null, credit: 0, creditUntil: Infinity, recovered: -Infinity, spans: new Set() };
}
function offsetFor(clock, frame) {
  let addMs = 0;
  for (const anchor of clock.anchors) if (anchor.fromFrame <= frame) addMs += anchor.addMs;
  return clock.clockOffset + addMs + frame / clock.rate * 1000;
}
// The tracked level at a render position.
const line = (clock, contextTime) => clock.level + clock.slope * (contextTime - clock.ref.contextTime) * 1000;
// A line kept aside (the one a rate change replaced) and its level at a render position.
const kept = clock => ({ level: clock.level, slope: clock.slope, contextTime: clock.ref.contextTime });
const along = (base, contextTime) => base.level + base.slope * (contextTime - base.contextTime) * 1000;
// The least-squares line of readings' levels over their render positions.
function fit(readings) {
  if (readings.length < 2) return null;
  const t0 = readings[0].contextTime, n = readings.length;
  let mt = 0, mx = 0, tt = 0, tx = 0;
  for (const reading of readings) { mt += (reading.contextTime - t0) * 1000 / n; mx += reading.x / n; }
  for (const reading of readings) { const t = (reading.contextTime - t0) * 1000 - mt; tt += t * t; tx += t * (reading.x - mx); }
  if (!(tt > 0)) return null;
  const slope = tx / tt;
  let residual = 0;
  for (const reading of readings) residual += (reading.x - mx - slope * ((reading.contextTime - t0) * 1000 - mt)) ** 2;
  // The slope's standard error, from the readings' scatter about the line.
  const sd = n > 2 ? Math.sqrt(residual / (n - 2)) : Infinity, se = sd / Math.sqrt(tt);
  const through = (withSlope, contextTime) => mx + withSlope * ((contextTime - t0) * 1000 - mt);
  return { slope, sd, se, tt, through, at: contextTime => through(slope, contextTime) };
}
const median = values => {
  const sorted = [...values].sort((a, b) => a - b), mid = sorted.length >> 1;
  return sorted.length % 2 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2;
};
// Whether values stand out from the rest: farther from their median than half a step or
// STALL_SPREAD times the median distance from it, whichever is larger. A discontinuity stands
// out from the values around it; jitter of any size spreads them all alike.
function outliers(values, candidates = values) {
  const center = median(values), bound = Math.max(STALL_STEP_MS / 2, STALL_SPREAD * median(values.map(value => Math.abs(value - center))));
  return candidates.some(value => Math.abs(value - center) > bound);
}
// Whether readings rise steadily: between consecutive ones the level moves by the slope times
// the rendered time, and no pair's move off the slope stands out from the others'.
function steady(readings, slope) {
  const moved = [];
  for (let i = 1; i < readings.length; i++) moved.push(readings[i].x - readings[i - 1].x - slope * (readings[i].contextTime - readings[i - 1].contextTime) * 1000);
  return !moved.length || !outliers(moved);
}
// The line follows the trail: while the in-band readings are steady it is the least-squares
// line of the last FOLLOW_READINGS of them, so it learns a rate from SETTLE_READINGS readings on,
// steady drift never leaves the band, and a rate that changes with no jump is followed within a
// few seconds instead of lagging behind a fit that still holds the old rate (a stall then is
// measured from the new rate, and the rate change is not misjudged as a step). Steadiness is
// judged over the whole trail, so a discontinuity anywhere in it keeps the line from following.
// Not on a reading that stands out from the line, as the readings before it sit around the
// line: it is suspect, maybe the first of a loss seen through the jitter, and the readings after
// it tell (a hold that opens next takes it). Not within STALL_RECOVER_MS of a re-anchor:
// rendering returning part of a stall is credit, not a rate.
function follow(clock, reading) {
  const trail = clock.trail;
  if (trail.length <= SETTLE_READINGS) return;
  const recent = trail.slice(-FOLLOW_READINGS);
  const off = recent.map(earlier => earlier.x - line(clock, earlier.contextTime));
  if (outliers(off.slice(0, -1), off.slice(-1))) { reading.suspect = true; return; }
  if (performance.now() - clock.recovered <= STALL_RECOVER_MS) return;
  const trend = fit(recent);
  if (!trend) return;
  // A slope known within FOLLOW_SE; until then the line keeps its own and is re-centred.
  const slope = trend.se <= FOLLOW_SE ? trend.slope : clock.slope;
  if (steady(trail, slope)) {
    clock.slope = slope; clock.level = trend.through(slope, reading.contextTime);
    // The jitter: the steady trail's scatter about its own line, once NOISE_READINGS long. It
    // outlives a re-anchor, as a stall does not change the device.
    if (trail.length >= NOISE_READINGS) clock.noise = fit(trail).sd;
  }
}
// Whether the held readings are drift, and the line that follows it.
function drift(clock, held) {
  const before = clock.trail, last = held.at(-1), trend = before.length >= SETTLE_READINGS ? fit(before) : null;
  if (trend && Math.abs(held.reduce((sum, reading) => sum + reading.x - trend.at(reading.contextTime), 0) / held.length) <= STALL_AGREE_MS) {
    const both = [...before, ...held], all = fit(both);
    if (Math.abs(all.slope) <= STALL_SLOPE + 2 * all.se && steady(both, all.slope)) return { slope: all.slope, level: all.at(last.contextTime), trail: both.slice(-TRAIL_READINGS) };
  }
  const own = held.length >= SETTLE_READINGS ? fit(held) : null, junction = before.at(-1);
  if (!own || !junction) return null;
  // The scatter a rate's readings show is the jitter: the trail's, once known, else their own. A
  // staircase (a train of stalls a few batches apart) scatters about its line far more: more
  // than twice the jitter and STALL_AGREE_MS / 2 is no rate, and the junction and the slope
  // allow for the jitter alone.
  const noise = clock.noise ?? own.sd, scatter = Math.min(own.sd, noise);
  if (own.sd > Math.max(STALL_AGREE_MS / 2, 2 * noise)) return null;
  if (Math.abs(own.slope) <= STALL_SLOPE + 2 * scatter / Math.sqrt(own.tt) && Math.abs(own.at(junction.contextTime) - junction.x) <= STALL_AGREE_MS + 2 * scatter && steady([junction, ...held], own.slope)) {
    return { slope: own.slope, level: own.at(last.contextTime), trail: held.slice(-TRAIL_READINGS), changed: true };
  }
  return null;
}
// Whether a hold that opened soon after a rate change is the rate going back to the line it
// replaced, off it by more than a step: the change was a stutter and the readings measure its
// loss. Their own line keeps nearer to the replaced slope than to the changed one.
function returned(clock, { base, slope }) {
  const held = clock.pending.readings, off = held.map(reading => ({ x: reading.x - along(base, reading.contextTime), contextTime: reading.contextTime }));
  const own = held.length >= SETTLE_READINGS ? fit(off) : null;
  if (!own || 2 * Math.abs(own.slope) > Math.abs(slope - base.slope)) return null;
  const step = off.reduce((sum, reading) => sum + reading.x, 0) / off.length;
  return step > STALL_STEP_MS ? { base, step } : null;
}
// What settling the pending rise or fall on `readings` would do, without doing it.
function settlement(clock, readings) {
  const { sign, since, provisional } = clock.pending, recovering = sign < 0 && since - clock.recovered <= STALL_RECOVER_MS;
  const stutter = provisional ? returned(clock, provisional) : null;
  const ramp = recovering || stutter ? null : drift(clock, clock.pending.readings);
  if (ramp) return { ramp, anchor: null };
  const steps = readings.map(reading => reading.x - line(clock, reading.contextTime));
  const step = stutter ? stutter.step : sign > 0 ? Math.min(...steps) : Math.max(...steps), taken = readings[steps.indexOf(step)];
  const total = (performance.now() <= clock.creditUntil ? clock.credit : 0) + step, before = clock.ref;
  if (!(total > 0)) return { step, taken, total, recovering, anchor: null };
  // Frames before the last fresh stamp were rendered before the stall: queued chunks of
  // them keep their mapping. Chunks a span already admitted are never moved.
  let held = Infinity;
  for (const span of clock.spans) if (span.heldFrom !== null) held = Math.min(held, span.heldFrom);
  const fromFrame = Math.max(before ? Math.round(before.contextTime * clock.rate) : 0, held === Infinity ? 0 : held);
  return { step, taken, stutter, total, anchor: { fromFrame, addMs: total } };
}
function settle(clock, readings) {
  const { ramp, step, taken, stutter, total, recovering, anchor } = settlement(clock, readings), last = readings.at(-1), { sign } = clock.pending;
  let rehold = null;
  if (ramp) {
    // A rate that changed at the junction is provisional for STALL_RECOVER_MS of rendering: a
    // stutter (stalls on consecutive batches) makes the same staircase, then goes back to the old rate.
    const base = kept(clock);
    ({ slope: clock.slope, level: clock.level, trail: clock.trail } = ramp);
    clock.provisional = ramp.changed && Math.abs(ramp.slope - base.slope) > STALL_STUTTER ? { base, slope: ramp.slope, contextTime: last.contextTime } : null;
  } else if (stutter) {
    // The line goes back to the rate the stutter interrupted, moved by what it lost.
    clock.slope = stutter.base.slope; clock.level = along(stutter.base, last.contextTime) + step;
    clock.trail = clock.pending.readings.slice(-TRAIL_READINGS); clock.provisional = null;
    if (anchor) { clock.anchors.push(anchor); clock.credit = 0; clock.recovered = performance.now(); }
    else clock.credit = total;
  } else {
    // The line moves by the step it was taken at, so a rise that went on past it (a
    // continuous loss) is still a deviation, judged next: when the latest reading sits beyond
    // the step by more than STALL_AGREE_MS, it opens a new hold at once (a second stall one
    // batch after the first), with the reading the step was taken at as its junction.
    clock.level = line(clock, last.contextTime) + step; clock.trail = [last]; clock.provisional = null;
    if (sign * (last.x - clock.level) > STALL_AGREE_MS) { clock.trail = [taken]; rehold = { sign, readings: [last], since: performance.now(), chained: true }; }
    if (anchor) { clock.anchors.push(anchor); clock.credit = 0; clock.recovered = performance.now(); }
    else {
      // Credit from a recovery offsets the next rise whenever it comes; from any other fall
      // (rendering ahead of the wall with no stall before it) only a rise within STALL_RECOVER_MS.
      clock.credit = total;
      if (recovering) { clock.recovered = performance.now(); clock.creditUntil = Infinity; }
      else if (step < 0) clock.creditUntil = performance.now() + STALL_RECOVER_MS;
    }
  }
  clock.ref = last; clock.pending = rehold; clock.seeded = true;
}
function read(clock, reading) {
  if (clock.level === null) { clock.level = reading.x; clock.ref = reading; clock.trail = [reading]; clock.seeded = true; return; }
  const threshold = STALL_STEP_MS + (clock.seeded ? 0 : clock.seedSlack), expected = line(clock, reading.contextTime), deviation = reading.x - expected;
  if (Math.abs(deviation) <= threshold) {
    if (!clock.seeded) clock.level = reading.x;
    else {
      const most = Math.max(0, reading.contextTime - clock.ref.contextTime) * 1000 * STALL_CENTER;
      clock.level = expected + Math.max(-most, Math.min(most, deviation));
    }
    clock.ref = reading; clock.pending = null; clock.seeded = true;
    clock.trail.push(reading);
    if (clock.trail.length > TRAIL_READINGS) clock.trail.shift();
    follow(clock, reading);
    return;
  }
  const sign = Math.sign(deviation), pending = clock.pending;
  if (!pending || pending.sign !== sign) {
    // A flip keeps the time the context has been unsettled: the bound is per hold, not per sign.
    if (pending) { clock.pending = { sign, readings: [reading], since: pending.since, flipped: true, provisional: pending.provisional }; return; }
    // Suspect readings just before a hold are its first: the line and the junction go back to
    // the last reading before them.
    const suspects = [];
    while (clock.trail.length > 1 && clock.trail.at(-1).suspect && Math.sign(clock.trail.at(-1).x - line(clock, clock.trail.at(-1).contextTime)) === sign) suspects.unshift(clock.trail.pop());
    if (suspects.length) { clock.level = line(clock, clock.trail.at(-1).contextTime); clock.ref = clock.trail.at(-1); }
    const readings = [...suspects, reading], provisional = clock.provisional;
    clock.pending = { sign, readings, since: performance.now(), provisional: provisional && (readings[0].contextTime - provisional.contextTime) * 1000 <= STALL_RECOVER_MS ? provisional : null };
    return;
  }
  const previous = pending.readings.findLast(earlier => reading.at - earlier.at >= STALL_CONFIRM_MS);
  pending.readings.push(reading);
  // Agreement is against the line: under drift, two readings of one step differ by the slope.
  const agree = previous && Math.abs(reading.x - previous.x - clock.slope * (reading.contextTime - previous.contextTime) * 1000) <= STALL_AGREE_MS;
  if (agree && judged(clock, [previous, reading], sign)) settle(clock, [previous, reading]);
}
// Whether a hold can be told apart yet. One a settle opened at once continues a discontinuity
// already confirmed. One after a provisional rate waits for SETTLE_READINGS readings, whose own
// line tells a stutter's return. With SETTLE_READINGS held, their own line says. Before
// that, only a discontinuity at the junction: the agreeing readings sit off the line by more than
// a step beyond where the last SETTLE_READINGS in-band readings sat. A rate that changed with
// no jump creeps up to the band, so its last in-band readings already sit near the held ones.
function judged(clock, readings, sign) {
  if (clock.pending.chained || clock.pending.readings.length >= SETTLE_READINGS) return true;
  if (clock.pending.provisional) return false;
  const recent = clock.trail.slice(-SETTLE_READINGS);
  if (recent.length < SETTLE_READINGS) return false;
  const off = reading => reading.x - line(clock, reading.contextTime);
  const step = sign > 0 ? Math.min(...readings.map(off)) : Math.max(...readings.map(off));
  return sign * (step - recent.reduce((sum, reading) => sum + off(reading), 0) / recent.length) > STALL_STEP_MS;
}
function observe(context, clock) {
  if (!clock?.anchors) return;
  const stamp = context.state === "running" ? context.getOutputTimestamp?.() : undefined;
  if (stamp?.contextTime > 0 && stamp?.performanceTime > 0 && stamp.contextTime !== clock.lastStampTime) {
    clock.lastStampTime = stamp.contextTime;
    read(clock, { x: stamp.performanceTime - stamp.contextTime * 1000 - latencyMs(context.outputLatency), contextTime: stamp.contextTime, at: performance.now() });
  }
  // Checked on events, never slept on.
  const pending = clock.pending;
  if (pending && performance.now() - pending.since >= SETTLE_MS) {
    // Readings on both sides of the level confirm nothing: release the hold with no step.
    if (pending.flipped) clock.pending = null;
    else settle(clock, pending.readings);
  }
  for (const span of [...clock.spans]) span.release();
}

// D61: the session origin is a wall-clock Date.now(). Map it onto this document's
// performance.now() once, so every later time is performance.now(),
// performanceTime or render frames: never performance.timeOrigin, which each
// document fixes on its own. Both methods place a render frame at the instant it
// reaches the output, as getOutputTimestamp does. The uncertainty is the part of the
// span's start error the extension measures; the browser's own latency estimates
// and input latency it never reports are outside it.
const latencyMs = value => Number.isFinite(value) && value > 0 ? value * 1000 : 0;
async function measure(context, node, origin, wait) {
  const a = Date.now(), p = performance.now(), b = Date.now();
  const originNow = p - ((a + b) / 2 - origin), rate = context.sampleRate, quantum = 128 / rate * 1000;
  let clockMethod = "outputTimestamp", clockOffset, uncertainty = b - a + 1 + quantum, level0, seedTime, seedSlack = 0;
  const stamp = context.getOutputTimestamp?.(), stampTime = stamp?.contextTime > 0 && stamp?.performanceTime > 0 ? stamp.contextTime : 0;
  if (stampTime) {
    clockOffset = stamp.performanceTime - originNow - stamp.contextTime * 1000;
    level0 = stamp.performanceTime - stamp.contextTime * 1000 - latencyMs(context.outputLatency);
    seedTime = stamp.contextTime;
  } else {
    // The worklet reads its render frame between t0 and t1, so half the round trip bounds
    // when; the frame moves once per device callback (baseLatency), and reaches the
    // output outputLatency after it is rendered.
    clockMethod = "roundTrip";
    const t0 = performance.now();
    const { frame, t1 } = await wait(new Promise(resolve => {
      node.port.onmessage = ({ data }) => { if (Number.isFinite(data?.clockFrame)) resolve({ frame: data.clockFrame, t1: performance.now() }); };
      node.port.postMessage("clock");
    }), "Audio render clock did not answer.");
    clockOffset = (t0 + t1) / 2 + latencyMs(context.outputLatency) - originNow - frame / rate * 1000;
    uncertainty += (t1 - t0) / 2 + Math.max(0, latencyMs(context.baseLatency) - quantum);
    level0 = (t0 + t1) / 2 - frame / rate * 1000;
    seedTime = frame / rate;
    seedSlack = Math.ceil(uncertainty);
  }
  // No PCM precedes currentTime. A negative start moves the whole span once, keeps
  // its spacing exact and is recorded; it proves an error at least that large. One a
  // day early is a fault, not a clamp. Chunks add the same start, so rounding cannot
  // leave the raised first chunk a hair below zero.
  const start = (context.currentTime || 0) * 1000, first = clockOffset + start, clockClampMs = first < 0 ? Math.ceil(-first) : 0;
  if (clockClampMs > 86400000) throw new Error("Audio timing fell before the recording start.");
  const raised = clockOffset + clockClampMs + start < 0 ? -start : clockOffset + clockClampMs;
  return { clockOffset: raised, clockMethod, originNow, rate, level0, seedSlack, seedTime, stampTime,
    clockUncertaintyMs: Math.min(86400000, Math.max(Math.ceil(uncertainty), clockClampMs)), ...(clockClampMs ? { clockClampMs } : {}) };
}
function calibrate(context, node, origin, wait) {
  if (!clocks.has(context)) {
    const task = measure(context, node, origin, wait).then(clockRecord);
    clocks.set(context, task);
    task.then(clock => { if (clocks.get(context) === task) clocks.set(context, clock); },
      () => { if (clocks.get(context) === task) clocks.delete(context); });
  }
  return Promise.resolve(clocks.get(context));
}

// A sender's mute (replaceTrack(null), inactive encodings or direction) keeps the microphone
// feeding the worklet, whose chunks reach this thread late. Each observed state is placed on
// the render clock: an observed mute covers every frame since the last observed unmute, and
// an observed unmute ends a guard of input latency later, so muted audio never passes.
export const MUTE_GUARD_MS = 50, MUTE_POLL_MS = 20;
export function muteGate(context, enabled) {
  const ranges = [], frameNow = () => Math.floor((context.currentTime || 0) * context.sampleRate);
  const guard = Math.round(context.sampleRate * MUTE_GUARD_MS / 1000);
  let open = null, lastOn = frameNow();
  function observe() {
    const at = frameNow();
    if (enabled()) {
      if (open) { open.end = at + guard; open = null; }
      lastOn = at;
    } else if (!open) ranges.push(open = { start: lastOn, end: Infinity });
  }
  function apply(frame, pcm, channels) {
    observe();
    const samples = new Int16Array(pcm), frames = samples.length / channels;
    for (const range of ranges) {
      const from = Math.max(frame, range.start) - frame, to = Math.min(frame + frames, range.end) - frame;
      if (to > from) samples.fill(0, from * channels, to * channels);
    }
    // Chunks arrive in render order: a closed range before this chunk's end never applies again.
    while (ranges.length && ranges[0] !== open && ranges[0].end <= frame + frames) ranges.shift();
  }
  observe();
  return { observe, apply, get ranges() { return ranges.length; } };
}

async function captureOnce({ stream, id, label, labelSource, kind, origin, workletURL, send, fail, audible = false, enabled, channels = 1, sharedContext, getSharedContext, warn = () => {}, sourceTrackId, reportOutcomes = false, onAudio = () => {}, schedule, cancel }) {
  sharedContext ||= getSharedContext?.();
  const context = sharedContext || new AudioContext({ sampleRate: 48000 });
  const wait = (promise, message) => bounded(promise, message, schedule, cancel);
  let source, node, silent, remotePlayback, stopping = false, ending = false, failure = null;
  let pendingFrames = 0, sequence = 0, flushed, droppedFrames = 0, clock = null;
  // D61.1: chunks wait here, counted in pendingFrames, while the context's clock is unsettled,
  // or while it has no output stamp yet to check its calibration against (bounded like a rise).
  const waiting = [], inFlight = new Set(), held = [];
  let verified = false, holdSince = null, holds = 0;
  const span = { heldFrom: null, release: (force = false) => release(force) };
  let stateChanged, announced = false, ended = false, endOffsetMs = null, gate = null, gateTimer = null;
  const later = schedule || setTimeout, unschedule = cancel || clearTimeout;
  const pollGate = () => {
    gateTimer = null;
    if (stopping || ending || failure) return;
    gate.observe();
    gateTimer = later(pollGate, MUTE_POLL_MS);
  };
  const end = async reason => {
    if (!reportOutcomes || !announced || ended) return;
    ended = true;
    try { await send({ type: "trackEnd", id, reason, droppedFrames, endOffsetMs }); }
    catch (error) { if (!retirementReason(error)) throw error; }
  };
  function pump() {
    while (waiting.length && inFlight.size < 4 && !failure && !ended) {
      const { message, frames } = waiting.shift();
      const task = Promise.resolve().then(() => send(message)).then(() => {
        endOffsetMs = Math.max(endOffsetMs ?? 0, message.offsetMs + frames * 1000 / context.sampleRate);
        if (!failure && !stopping && !ending && !ended) onAudio();
      }).catch(abort).finally(() => {
        pendingFrames -= frames; inFlight.delete(task); pump();
      });
      inFlight.add(task);
    }
  }
  function abort(error) {
    if (failure) return;
    failure = error;
    waiting.length = 0; held.length = 0; span.heldFrom = null;
    fail(error);
  }
  function admit(frame, pcm, frames, mapping = clock) {
    // Render frames space every chunk; arrival on the main thread never times one.
    const offsetMs = offsetFor(mapping, frame);
    if (!(offsetMs >= 0)) {
      droppedFrames += frames; pendingFrames -= frames;
      abort(new Error("Audio timing fell before the recording start."));
      return;
    }
    if (endOffsetMs !== null && offsetMs - endOffsetMs > 60000) {
      droppedFrames += frames; pendingFrames -= frames;
      abort(new Error("A suspended source needs a new audio span."));
      return;
    }
    const message = { type: "audio", id, sequence: sequence++, offsetMs, pcm: base64(pcm) };
    waiting.push({ message, frames });
    pump();
  }
  function settled() {
    if (clock.pending) return false;
    if (!verified && (clock.lastStampTime > 0 || holds >= SETTLE_READINGS || performance.now() - holdSince >= SETTLE_MS)) verified = true;
    return verified;
  }
  function release(force, mapping = clock) {
    if (!held.length || failure) return;
    // A hold that a settle opened at once (the loss went on) moves no frame before its first
    // reading, so the chunks before it go now: a stutter holds none longer than one hold.
    const pending = clock.pending, before = force || settled() ? Infinity : pending?.chained && verified ? Math.round(pending.readings[0].contextTime * clock.rate) : -Infinity;
    while (held.length && !failure && held[0].frame < before) {
      const chunk = held.shift();
      span.heldFrom = held[0]?.frame ?? null;
      admit(chunk.frame, chunk.pcm, chunk.frames, mapping);
    }
  }
  try {
    await wait(context.audioWorklet.addModule(workletURL), "This meeting page blocks separate-track recording. Record the whole tab instead.").catch(classified("workletBlocked"));
    // Chromium may not decode a receiver for Web Audio until a media renderer exists.
    // Keep a silent renderer alive (crbug.com/40094084), without playing remote audio twice.
    if (kind === "remote") {
      remotePlayback = new Audio();
      remotePlayback.muted = true;
      remotePlayback.srcObject = stream;
      await wait(remotePlayback.play(), "A participant’s audio did not start. Record the whole tab instead.").catch(classified("audioBlocked"));
    }
    source = context.createMediaStreamSource(stream);
    node = new AudioWorkletNode(context, "cassini-pcm", {
      channelCount: channels, channelCountMode: "explicit", outputChannelCount: [channels],
      processorOptions: { channels }
    });
    silent = context.createGain();
    silent.gain.value = 0;
    // A newly constructed worklet can observe its initial global frame before
    // the device clock catches up. Warm only the disconnected graph; no source
    // PCM is admitted or timestamp adjusted during this readiness handshake.
    const ready = new Promise(resolve => { node.port.onmessage = ({data}) => {if (data.ready) resolve();}; });
    node.connect(silent).connect(context.destination);
    keepRealTime(context);
    if (audible) source.connect(context.destination);
    await wait(context.resume(), "Click somewhere in the meeting page so the browser allows audio, then try again. Or record the whole tab.").catch(classified("audioBlocked"));
    if (context.state !== "running") throw Object.assign(new Error("Audio capture is suspended. Interact with the meeting page, then try again or use Tab audio."), { resultCode: "audioBlocked" });
    await wait(ready, "Audio render clock did not become ready.");
    source.connect(node);
    const cached = clocks.has(context);
    clock = await calibrate(context, node, origin, wait);
    holdSince = performance.now();
    clock.spans.add(span);
    // A later span takes its own reading, so it never starts on a mapping a stall made stale.
    if (cached) observe(context, clock);
    await send({ type: "track", id, label, ...(labelSource ? {labelSource} : {}), kind, sampleRate: context.sampleRate, channels,
      ...(sourceTrackId ? { sourceTrackId } : {}), clockMethod: clock.clockMethod, clockUncertaintyMs: clock.clockUncertaintyMs,
      ...(clock.clockClampMs ? { clockClampMs: clock.clockClampMs } : {}) });
    announced = true;
    node.onprocessorerror = () => abort(new Error("Audio capture stopped unexpectedly."));
    stateChanged = () => {
      if (stopping || ending || context.state === "running") return;
      if (["suspended", "interrupted"].includes(context.state)) {
        void resumeSuspended(context).catch(abort);
      } else abort(new Error("Browser audio capture closed unexpectedly."));
    };
    context.addEventListener?.("statechange", stateChanged);
    node.port.onmessage = ({ data }) => {
      if (data.stopped) { if (stopping) ending = true; flushed?.(); return; }
      if (failure || ending || ended) return;
      // Every arrival reads the clock first, so a dropped chunk still feeds the detector.
      observe(context, clock);
      if (failure || ending || ended) return;
      const frames = data.pcm.byteLength / (channels * 2);
      if (pendingFrames + frames > context.sampleRate * 10) {
        droppedFrames += frames;
        warn("One track fell more than ten seconds behind and has a gap. The other tracks continue.", {kind:"gap"});
        return;
      }
      pendingFrames += frames;
      // For sender tracks, respect track enabled/muted state and sender suspension.
      gate?.apply(data.frame, data.pcm, channels);
      if (!held.length && settled()) { admit(data.frame, data.pcm, frames); return; }
      held.push({ frame: data.frame, pcm: data.pcm, frames });
      span.heldFrom ??= data.frame;
      holds++;
      release(false);
    };
    if (enabled) { gate = muteGate(context, enabled); pollGate(); }
    node.port.postMessage("start");
    return {
      observe() { if (!stopping && !ending && !failure) gate?.observe(); },
      get failed() { return !!failure; },
      get droppedFrames() { return droppedFrames; },
      async stop(reason = "stopped") {
        if (stopping) return;
        stopping = true;
        if (gateTimer !== null) { unschedule(gateTimer); gateTimer = null; }
        if (context.state === "running") {
          await new Promise((resolve, reject) => {
            const timer = setTimeout(() => reject(new Error("Audio capture did not finish in time.")), 2000);
            flushed = () => { clearTimeout(timer); resolve(); };
            node.port.postMessage("stop");
          }).catch(abort);
        }
        // Stop is no evidence that a pending rise is real. Release this span on the
        // committed clock, so a successor cannot start inside a speculative tail.
        // Other and later spans keep judging the rise on the shared context.
        observe(context, clock);
        release(true);
        // Accept the worklet's final flush through its stopped barrier, then
        // fence even a retained callback before draining admitted PCM.
        ending = true;
        clock.spans.delete(span);
        node.port.onmessage = null; node.onprocessorerror = null;
        source.disconnect(); node.disconnect(); silent.disconnect();
        if (remotePlayback) { remotePlayback.pause(); remotePlayback.srcObject = null; }
        while (inFlight.size) await Promise.allSettled([...inFlight]);
        waiting.length = 0;
        context.removeEventListener?.("statechange", stateChanged);
        if (!sharedContext) await context.close();
        await end(retirementReason(failure) || (reason === "retry" ? "retry" : failure ? "failed" : reason));
        if (failure && !retirementReason(failure)) throw failure;
      }
    };
  } catch (error) {
    stopping = ending = true;
    if (gateTimer !== null) { unschedule(gateTimer); gateTimer = null; }
    if (node) { node.port.onmessage = null; node.onprocessorerror = null; }
    waiting.length = 0; held.length = 0;
    clock?.spans.delete(span);
    source?.disconnect(); node?.disconnect(); silent?.disconnect();
    if (remotePlayback) { remotePlayback.pause(); remotePlayback.srcObject = null; }
    context.removeEventListener?.("statechange", stateChanged);
    if (!sharedContext) await context.close();
    await end(retirementReason(error) || "failed").catch(() => {});
    throw error;
  }
}


// The same policy is exercised with a fake monotonic clock in both capture and
// discovery tests. Quiet PCM is healthy; only transport/source failures retry.
export function retryDelay(attempts) {
  return [1000, 5000, 15000][Math.max(0, attempts)] ?? 30000;
}

export function reconcile(state, attempts, lastHealthyAt, now) {
  if (state.stopping || state.live === false) return "giveUpAndReport";
  if (state.status === "recording") return "none";
  if (state.failedAt === null || state.failedAt === undefined) return "reattach";
  const recovered = lastHealthyAt !== null && lastHealthyAt !== undefined && state.failedAt - lastHealthyAt >= 60000;
  return now >= state.failedAt + retryDelay(recovered ? 0 : attempts) ? "reattach" : "none";
}

export async function capture(options) {
  let current = null, ending = false, terminal = null, retryTimer = null, retries = 0;
  let recovering = false, retiring = false, retiredDrops = 0, healthySince = null;
  let restarting = Promise.resolve();
  const warn = options.warn || (() => {});
  const now = options.clock || (() => performance.now());
  const later = options.schedule || ((fn, ms) => setTimeout(fn, ms));
  const cancel = options.cancel || (timer => clearTimeout(timer));
  const live = options.live || (() => {
    const tracks = options.stream.getAudioTracks?.();
    return !tracks || tracks.some(track => track.readyState === "live");
  });
  const healthy = () => {
    if (recovering) warn(undefined, {kind:"recovered", sourceTrackId:options.sourceTrackId || options.id});
    recovering = false;
    healthySince ??= now();
    if (now() - healthySince >= 60000) retries = 0;
    options.onAudio?.();
  };
  const schedule = value => {
    if (ending || retiring || terminal || retryTimer !== null) return;
    const error = value instanceof Error ? value : new Error(String(value));
    if (retirementReason(error) && retirementReason(error) !== "ended") {
      // The token is deliberately fenced or already saved by the native app.
      retiring = true;
      if (retryTimer !== null) cancel(retryTimer);
      retryTimer = null;
      return;
    }
    const at = now();
    if (error.code === "limit" || reconcile({ live: live(), failedAt: at }, retries, healthySince, at) === "giveUpAndReport") {
      terminal = error; options.fail?.(error.message); return;
    }
    if (healthySince !== null && at - healthySince >= 60000) retries = 0;
    healthySince = null;
    recovering = true;
    warn(error.message, {kind:"retry", sourceTrackId:options.sourceTrackId || options.id});
    const delay = options.retryDelayMs ?? retryDelay(retries);
    retryTimer = later(async () => {
      retryTimer = null;
      restarting = (async () => {
        const previous = current; current = null;
        retiredDrops += previous?.droppedFrames || 0;
        await previous?.stop("retry").catch(() => {});
        if (ending) return;
        if (!live()) { terminal = error; options.fail?.(error.message); return; }
        retries++;
        try {
          const id = options.nextSpanId ? options.nextSpanId() : crypto.randomUUID();
          if (!id) throw Object.assign(new Error("This recording has reached its track limit."), {code:"limit",scope:"source"});
          current = await captureOnce({ ...options, id, fail: schedule, onAudio: healthy });
        } catch (next) { schedule(next); }
      })();
      await restarting;
    }, delay);
  };
  try { current = await captureOnce({ ...options, fail: schedule, onAudio: healthy }); }
  catch (error) { schedule(error); }
  return {
    get failed() { return !!terminal; },
    get state() { return ending || retiring ? "stopped" : terminal ? "failed" : recovering ? "retrying" : "recording"; },
    get droppedFrames() { return retiredDrops + (current?.droppedFrames || 0); },
    observe() { current?.observe?.(); },
    async stop(reason = "stopped") {
      if (ending) return;
      ending = true;
      if (retryTimer !== null) cancel(retryTimer);
      retryTimer = null;
      await restarting;
      try { await current?.stop(reason); } catch (error) { if (terminal) throw error; warn(error.message); }
      if (terminal) throw terminal;
    }
  };
}
