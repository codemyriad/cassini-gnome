// The driver's session clock (D68 shared test 2). SPEC: the session origin is a wall-clock
// Date.now(); a capturing document maps it onto its own monotonic clock once and never reads the
// wall clock again. The driver does the same, so its requested and observed times share the
// apps' monotonic timeline: a wall clock that drifts or steps during a run (a hosted VM's ran
// 500 ppm fast) can no longer move them.
//
// monotonic() is the system's monotonic clock in milliseconds (process.hrtime:
// CLOCK_MONOTONIC on Linux, mach_absolute_time on macOS). Unlike performance.now(), which each
// process counts from its own start, it is one clock for every process on the machine, so the
// control relay's frame stamps and the driver's readings compare directly.
export const monotonic = () => Number(process.hrtime.bigint()) / 1e6;

// Maps a wall-clock origin (Date.now() ms) onto the monotonic clock once: one wall reading
// between two monotonic ones. at(m) is the session time of a monotonic reading m; now() reads it.
export function sessionClock(originMs, { wall = Date.now, mono = monotonic } = {}) {
  const before = mono(), atWall = wall(), after = mono();
  const originMono = (before + after) / 2 - (atWall - originMs);
  return { originMono, at: m => m - originMono, now: () => mono() - originMono };
}
