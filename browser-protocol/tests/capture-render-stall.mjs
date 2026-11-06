// D61.1 regression check (R93 finding 1): a real render stall of a capture AudioContext, and
// what the production capture() (extension/capture.js + pcm-worklet.js, served as the page loads
// them) then stamps on each chunk. It passes only when the stall is re-anchored (see the verdict).
//
// A meeting frame shares one AudioContext and one calibration (D61). This opens source A on a
// shared capture context, waits for PRE seconds of A's audio, then stops a Chromium process with
// SIGSTOP for STALL seconds (STALL_TARGET: "audio" = the out-of-process audio service, "renderer"
// = every renderer process, which hosts the page, its AudioContexts and the AudioWorklet thread),
// and SIGCONT. After POST seconds of A's audio past the resume it starts source B on the same
// shared context (calibration cached) and control source C on a fresh context (its own
// calibration), and runs until B and C each have TAIL seconds.
//
// Truth is the page's monotonic clock: origin = Date.now() is read next to performance.now()
// once, and every chunk's arrival in send() is performance.now() - that reading. A chunk arrives
// after its last frame was rendered, so offsetMs + duration - arrival is a steady negative
// latency (about one 8192-frame batch); a stall that loses rendered time moves it down for good.
// A sampler (diagnostic cadence, not a wait) records currentTime, getOutputTimestamp(),
// outputLatency/baseLatency, state and playoutStats every SAMPLE_MS.
//
// Every wait is on an event (an audio count reached or a failure) bounded by one deadline; the
// only timer that is not a bound is the stall itself, which is the stimulus.
//
// Verdict, from the page's monotonic clock (lag = offsetMs + duration - arrival):
// - A's lag after the stall settles is within 60 ms of its lag before it;
// Jumps are offset steps beyond render-frame spacing of 20 ms or more: the host's gap tolerance
// (rate/50), below which no gap is written. A smaller step is a worklet quantum the browser did
// not process (seen once, 128 frames on B), which the offsets place correctly.
// - A has no backward jump and at most one forward jump; when the output stamp level
//   (performanceTime - contextTime - outputLatency) rose by more than 50 ms, exactly one, equal to
//   that rise within 30 ms; a run without a stall has no jump on any source;
// - the stall happened to this browser: the victims are processes of the Chromium this script
//   launched (a unique switch on its command line, a descendant of this process), each was seen
//   stopped, and the shared context's samples show the render clock frozen, or the page silent,
//   for at least half the stall;
// - B (the shared context's later span) is on A's mapping exactly: offsetMs - frame/rate of its
//   first five chunks is within 1 ms of A's at the nearest earlier frame (the worklet's frames,
//   read beside capture.js), and it never jumps;
// - C (its own context) starts where A's clock is: the upper envelope of the lags of its first ten
//   chunks is within 60 ms of A's over the same arrivals (a busy main thread only delays an
//   arrival, so the envelope ignores it), and it never jumps.
// Start minus request is reported; it includes the span's setup time (on a loaded machine over
// 100 ms), so it is no measure of placement.
//
// Output: JSON lines in $STALL_OUT (default capture-render-stall.jsonl in the temp directory), a
// verdict on stdout, exit status 1 on a failed check.
import { createServer } from 'node:http';
import { readFile, readdir, writeFile, appendFile } from 'node:fs/promises';
import { randomUUID } from 'node:crypto';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
// The stall stops Chromium processes found through /proc: Linux only.
if (process.platform !== 'linux') { console.log('capture-render-stall: skipped, needs Linux /proc'); process.exit(0); }
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || 'playwright');

const RATE = 48000;
const PRE = Number(process.env.PRE || 6);
const STALL = Number(process.env.STALL ?? 2);
const POST = Number(process.env.POST || 6);
const TAIL = Number(process.env.TAIL || 6);
const TARGET = process.env.STALL_TARGET || 'audio';
const SAMPLE_MS = Number(process.env.SAMPLE_MS || 50);
const OUT = process.env.STALL_OUT || path.join(os.tmpdir(), 'capture-render-stall.jsonl');
const BOUND = (PRE + STALL + POST + TAIL + 60) * 1000;
const extraArgs = (process.env.CHROMIUM_ARGS || '').split(' ').filter(Boolean);
// The browser is found by this switch, which Chromium ignores, never by its name: any other
// Chromium on the machine (a desktop browser, a concurrent job on a shared runner) is not ours.
const MARKER = `--cassini-render-stall-gate=${process.pid}-${randomUUID()}`;

await writeFile(OUT, '');
const rows = [];
const log = record => { rows.push(record); return appendFile(OUT, JSON.stringify(record) + '\n'); };

const server = createServer(async (request, response) => {
  if (request.url === '/') { response.end('<!doctype html><title>Capture render stall</title><body>'); return; }
  if (!['/capture.js', '/pcm-worklet.js'].includes(request.url)) { response.writeHead(404).end(); return; }
  response.setHeader('Content-Type', 'text/javascript');
  response.end(await readFile(path.join(root, 'extension', request.url.slice(1))));
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const address = `http://127.0.0.1:${server.address().port}/`;

// Our Chromium's process tree from /proc: the browser is the one process that carries MARKER and
// descends from this script; the tree is its descendants, with their --type.
async function processes() {
  const all = [];
  for (const name of await readdir('/proc')) {
    if (!/^\d+$/.test(name)) continue;
    try {
      const stat = await readFile(`/proc/${name}/stat`, 'utf8');
      const ppid = Number(stat.slice(stat.lastIndexOf(')') + 2).split(' ')[1]);
      const cmd = (await readFile(`/proc/${name}/cmdline`, 'utf8')).split(/[\0 ]/);
      all.push({ pid: Number(name), ppid, cmd });
    } catch { /* exited */ }
  }
  const byPid = new Map(all.map(p => [p.pid, p]));
  const ours = p => { for (let q = p, n = 0; q && n < 64; q = byPid.get(q.ppid), n++) if (q.ppid === process.pid) return true; return false; };
  const browsers = all.filter(p => p.cmd.includes(MARKER) && ours(p));
  if (browsers.length !== 1) throw new Error(`expected one browser with ${MARKER} under pid ${process.pid}, found ${JSON.stringify(browsers.map(p => p.pid))}`);
  const browserPid = browsers[0].pid, tree = new Set([browserPid]);
  for (let grew = true; grew;) {
    grew = false;
    for (const p of all) if (!tree.has(p.pid) && tree.has(p.ppid)) { tree.add(p.pid); grew = true; }
  }
  return all.filter(p => tree.has(p.pid)).map(p => ({
    pid: p.pid,
    type: (p.cmd.find(a => a.startsWith('--type=')) || '--type=browser').slice(7),
    sub: (p.cmd.find(a => a.startsWith('--utility-sub-type=')) || '').slice(19),
  }));
}

const counts = { A: 0, B: 0, C: 0 };
const waiters = [];
let failure = null;
const until = (predicate, what) => new Promise((resolve, reject) => {
  const waiter = { predicate, resolve, reject, what };
  if (failure) reject(failure); else if (predicate()) resolve(); else waiters.push(waiter);
});
const poke = () => {
  for (const w of [...waiters]) {
    if (failure) { w.reject(failure); waiters.splice(waiters.indexOf(w), 1); }
    else if (w.predicate()) { w.resolve(); waiters.splice(waiters.indexOf(w), 1); }
  }
};
const failWith = error => { failure ??= error; poke(); };

let browser, deadline;
try {
  browser = await chromium.launch({ headless: true, ...(process.env.BROWSER_CHANNEL ? { channel: process.env.BROWSER_CHANNEL } : {}), args: ['--autoplay-policy=no-user-gesture-required', MARKER, ...extraArgs] });
  const page = await browser.newPage();
  page.on('console', m => log({ kind: 'console', text: m.text() }));
  await page.goto(address);
  deadline = setTimeout(() => failWith(new Error(`bound ${BOUND} ms reached: ${JSON.stringify(counts)}`)), BOUND);
  await page.exposeFunction('report', batch => {
    for (const r of batch) {
      if (r.kind === 'chunk') counts[r.src] += r.frames / RATE;
      void log(r);
    }
    poke();
  });
  await page.exposeFunction('reportFailure', (src, message) => failWith(new Error(`${src} capture failed: ${message}`)));
  const procs = await processes();
  await log({ kind: 'processes', marker: MARKER, procs });
  const info = await page.evaluate(async ({ SAMPLE_MS }) => {
    // Each worklet's batch frames, read beside capture.js (its port's message listener), so a
    // chunk's offset can be compared by render frame. Nodes are listed per context in order.
    window.lab = { nodes: [] };
    const Worklet = window.AudioWorkletNode;
    window.AudioWorkletNode = class extends Worklet {
      constructor(...args) {
        super(...args);
        const record = { context: args[0], frames: [] };
        window.lab.nodes.push(record);
        this.port.addEventListener('message', ({ data }) => { if (data?.pcm) record.frames.push(data.frame); });
      }
    };
    const { capture } = await import('/capture.js');
    const a = Date.now(), p = performance.now(), b = Date.now();
    const origin = Math.round((a + b) / 2), originPerf = p - ((a + b) / 2 - origin);
    const session = () => performance.now() - originPerf;
    Object.assign(window.lab, { origin, originPerf, captures: {}, contexts: {}, buffer: [] });
    const flush = () => { if (window.lab.buffer.length) { const out = window.lab.buffer; window.lab.buffer = []; window.report(out); } };
    const push = r => { window.lab.buffer.push(r); if (r.kind === 'chunk') flush(); };
    const shared = new AudioContext({ sampleRate: 48000 });
    window.lab.contexts.shared = shared;
    const stat = context => {
      const ots = context.getOutputTimestamp?.();
      const ps = context.playoutStats;
      return { ct: context.currentTime, otsCt: ots?.contextTime, otsPt: ots?.performanceTime === undefined ? undefined : ots.performanceTime - originPerf,
        state: context.state, outLat: context.outputLatency, baseLat: context.baseLatency,
        ...(ps ? { ps: { fb: ps.fallbackDuration, fbN: ps.fallbackFramesEvents, total: ps.totalDuration, lat: ps.averageLatency } } : {}) };
    };
    // A diagnostic sampler on each capture context (cadence, not a wait).
    setInterval(() => {
      for (const [name, context] of Object.entries(window.lab.contexts)) push({ kind: 'sample', ctx: name, at: session(), ...stat(context) });
      if (window.lab.buffer.length > 40) flush();
    }, SAMPLE_MS);
    shared.onstatechange = () => push({ kind: 'state', ctx: 'shared', at: session(), state: shared.state });
    const input = async (freq) => {
      const context = new AudioContext({ sampleRate: 48000 }), output = context.createMediaStreamDestination();
      const oscillator = context.createOscillator(); oscillator.frequency.value = freq; oscillator.connect(output); oscillator.start();
      await context.resume();
      return output.stream;
    };
    window.lab.start = async (src, ctx, freq) => {
      const context = ctx === 'shared' ? shared : new AudioContext({ sampleRate: 48000 });
      if (ctx !== 'shared') { window.lab.contexts[ctx] = context; context.onstatechange = () => push({ kind: 'state', ctx, at: session(), state: context.state }); }
      const requested = session();
      const send = async packet => {
        const at = session();
        if (packet.type === 'audio') {
          const bytes = atob(packet.pcm).length;
          push({ kind: 'chunk', src, ctx, seq: packet.sequence, offsetMs: packet.offsetMs, frames: bytes / 2, at, ...stat(context) });
        } else push({ kind: 'packet', src, ctx, at, packet });
      };
      window.lab.captures[src] = await capture({ stream: await input(freq), id: src, label: src, kind: 'remote', origin,
        workletURL: location.origin + '/pcm-worklet.js', send, sharedContext: context,
        fail: message => window.reportFailure(src, String(message)),
        warn: (message, details) => push({ kind: 'warn', src, at: session(), message: String(message), details }) });
      push({ kind: 'started', src, ctx, requested, at: session() });
      flush();
    };
    return { origin, hasPlayoutStats: 'playoutStats' in shared, ua: navigator.userAgent };
  }, { SAMPLE_MS });
  await log({ kind: 'info', ...info, PRE, STALL, POST, TAIL, TARGET, extraArgs, channel: process.env.BROWSER_CHANNEL || 'headless-shell', pulse: !!process.env.PULSE_SERVER });
  const sessionNow = () => Date.now() - info.origin;

  await page.evaluate(() => window.lab.start('A', 'shared', 440));
  await until(() => counts.A >= PRE, `A ${PRE} s`);

  const victims = (await processes()).filter(p => TARGET === 'audio' ? p.sub.includes('audio') : p.type === TARGET);
  if (!victims.length) throw new Error(`no ${TARGET} process in ${JSON.stringify(await processes())}`);
  const stoppedAt = sessionNow();
  if (STALL > 0) for (const v of victims) process.kill(v.pid, 'SIGSTOP');
  // Each victim is seen stopped (state T), bounded by one second.
  const state = async pid => { const stat = await readFile(`/proc/${pid}/stat`, 'utf8'); return stat.slice(stat.lastIndexOf(')') + 2, stat.lastIndexOf(')') + 3); };
  const stopBound = Date.now() + 1000;
  if (STALL > 0) for (const v of victims) {
    while ((v.state = await state(v.pid)) !== 'T') {
      if (Date.now() > stopBound) throw new Error(`victim ${v.pid} not stopped: state ${v.state}`);
      await new Promise(resolve => setImmediate(resolve));
    }
  }
  await log({ kind: 'stall', phase: 'stop', at: stoppedAt, victims, aSeconds: counts.A });
  if (STALL > 0) await new Promise(resolve => setTimeout(resolve, STALL * 1000));  // the stimulus, not a wait
  const resumedAt = sessionNow();
  if (STALL > 0) for (const v of victims) process.kill(v.pid, 'SIGCONT');
  await log({ kind: 'stall', phase: 'cont', at: resumedAt, aSeconds: counts.A });
  const aAtResume = counts.A;
  await until(() => counts.A >= aAtResume + POST, `A ${POST} s after resume`);

  await page.evaluate(() => Promise.all([window.lab.start('B', 'shared', 660), window.lab.start('C', 'fresh', 880)]));
  await until(() => counts.B >= TAIL && counts.C >= TAIL, `B and C ${TAIL} s`);
  await page.evaluate(() => Promise.all(Object.values(window.lab.captures).map(c => c.stop())));
  // A's and B's worklets are the shared context's first and second, C's the fresh context's only.
  const frames = await page.evaluate(() => {
    const on = context => window.lab.nodes.filter(node => node.context === context).map(node => node.frames);
    const [a, b] = on(window.lab.contexts.shared), [c] = on(window.lab.contexts.fresh);
    return { A: a, B: b, C: c };
  });
  await log({ kind: 'frames', frames });
  await log({ kind: 'done', at: sessionNow(), counts });
  const result = verdict();
  await log({ kind: 'verdict', ...result });
  console.log(`capture-render-stall: target=${TARGET} stall=${STALL}s ${result.pass ? 'PASS' : 'FAIL'} ${JSON.stringify(result)}; log ${OUT}`);
  if (!result.pass) process.exitCode = 1;
} catch (error) {
  await log({ kind: 'error', message: String(error?.stack || error) });
  console.error(error);
  process.exitCode = 1;
} finally {
  clearTimeout(deadline);
  await browser?.close();
  server.close();
}

function verdict() {
  const median = values => { const v = [...values].sort((a, b) => a - b); return v.length ? (v[(v.length - 1) >> 1] + v[v.length >> 1]) / 2 : NaN; };
  const stop = rows.find(r => r.kind === 'stall' && r.phase === 'stop').at, settle = rows.find(r => r.kind === 'stall' && r.phase === 'cont').at + 2000;
  const chunks = src => rows.filter(r => r.kind === 'chunk' && r.src === src);
  const lag = c => c.offsetMs + c.frames * 1000 / RATE - c.at;
  const jumps = src => chunks(src).slice(1).map((c, i, all) => { const p = chunks(src)[i]; return c.offsetMs - p.offsetMs - p.frames * 1000 / RATE; })
    .filter(d => Math.abs(d) >= 20).map(d => Math.round(d * 10) / 10);
  const a = chunks('A'), firstA = a[0].at;
  const level = r => r.otsPt - r.otsCt * 1000 - (r.outLat > 0 ? r.outLat * 1000 : 0);
  const samples = rows.filter(r => r.kind === 'sample' && r.ctx === 'shared' && r.otsCt > 0 && r.state === 'running');
  const stampStep = median(samples.filter(r => r.at > settle).map(level)) - median(samples.filter(r => r.at > firstA && r.at < stop).map(level));
  const lagShift = median(a.filter(c => c.at > settle).map(lag)) - median(a.filter(c => c.at < stop).map(lag));
  const start = src => { const s = rows.find(r => r.kind === 'started' && r.src === src); return chunks(src)[0].offsetMs - s.requested; };
  // The stall as the page saw it: the shared render clock frozen, or the page itself silent.
  const cont = rows.find(r => r.kind === 'stall' && r.phase === 'cont').at;
  const seen = rows.filter(r => r.kind === 'sample' && r.ctx === 'shared'), around = seen.filter((r, i) => seen[i + 1]?.at >= stop - 100 && r.at <= cont + 1000);
  let stallSeen = 0;
  for (let i = 1, frozen = 0; i < around.length; i++) {
    stallSeen = Math.max(stallSeen, around[i].at - around[i - 1].at);
    if (around[i].ct === around[frozen].ct) stallSeen = Math.max(stallSeen, around[i].at - around[frozen].at); else frozen = i;
  }
  const out = { stallSeen: Math.round(stallSeen), stampStep: Math.round(stampStep * 10) / 10, lagShift: Math.round(lagShift * 10) / 10,
    jumps: { A: jumps('A'), B: jumps('B'), C: jumps('C') }, startMinusRequest: { B: Math.round(start('B') * 10) / 10, C: Math.round(start('C') * 10) / 10 } };
  const failed = [];
  if (STALL > 0 && !(stallSeen >= STALL * 500)) failed.push(`the page saw no stall of at least half ${STALL} s`);
  if (!(Math.abs(lagShift) <= 60)) failed.push('A lag after the stall moved more than 60 ms');
  if (out.jumps.A.some(d => d < 0) || out.jumps.A.length > 1) failed.push('A: a backward jump or more than one jump');
  if (STALL === 0 && Object.values(out.jumps).some(j => j.length)) failed.push('a jump without a stall');
  if (STALL > 0 && stampStep > 50 && !(out.jumps.A.length === 1 && Math.abs(out.jumps.A[0] - stampStep) <= 30)) failed.push('A: no single jump equal to the stamp step');
  // B: offsetMs - frame/rate is the context's mapping at that frame (clockOffset plus anchors).
  const { frames } = rows.find(r => r.kind === 'frames');
  const base = (src, i) => chunks(src)[i].offsetMs - frames[src][i] * 1000 / RATE;
  for (const src of ['A', 'B']) if (!(frames[src]?.length >= chunks(src).length)) failed.push(`${src}: fewer worklet frames than chunks`);
  const bBase = chunks('B').slice(0, 5).map((_, i) => {
    let j = -1;
    for (let k = 0; k < chunks('A').length && frames.A[k] <= frames.B[i]; k++) j = k;
    return j < 0 ? NaN : base('B', i) - base('A', j);
  });
  out.mappingVsA = { B: Math.round(Math.max(...bBase.map(Math.abs)) * 1000) / 1000 };
  if (!(out.mappingVsA.B <= 1)) failed.push('B is not on A\'s mapping');
  // C: the upper envelope of the lags, which main-thread delays cannot raise.
  const firstC = chunks('C').slice(0, 10), from = firstC[0].at - 1000, to = firstC.at(-1).at + 1000;
  const relative = Math.max(...firstC.map(lag)) - Math.max(...a.filter(c => c.at >= from && c.at <= to).map(lag));
  out.lagVsA = { C: Math.round(relative * 10) / 10 };
  if (!(Math.abs(relative) <= 60)) failed.push('C starts more than 60 ms from A\'s clock');
  for (const src of ['B', 'C']) if (out.jumps[src].length) failed.push(`${src} jumps`);
  return { pass: failed.length === 0, failed, ...out };
}
