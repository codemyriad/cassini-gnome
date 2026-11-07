// Native messaging host launchers that record each host's pid (shared test 2, D68).
// The browser starts the launcher; it writes its own pid to <pidDirectory>/<name>-<pid>.pid
// and then execs the app's production host, so the pid recorded is the host's own process
// (its native port, its transport). Test code can then end exactly that process without
// knowing anything about the product. POSIX sh only, so the same launcher works on Linux
// and macOS.
//
// With `observe` (the control host, conformance/live-folder scenario.json resumeObservation)
// the launcher also numbers this launch (the first free <observe>/<n>, made with mkdir, so
// connections count from 1 in launch order), records "$$ $PPID" there, makes a FIFO in that
// private directory, starts scenarios/control-relay.mjs detached from itself (stdin
// /dev/null, stdout the browser's pipe it inherited, input the FIFO) and execs the host
// with only its stdout redirected into the FIFO. The host keeps the launcher's pid, parent,
// argv, environment and stdin, so it stays the browser's direct child; the browser-to-host
// direction is not touched. controlPipe() reads what the relays logged.
import { chmod, mkdir, readdir, readFile, stat, writeFile } from 'node:fs/promises';
import { execFile } from 'node:child_process';
import path from 'node:path';

const NAME = /^[a-z0-9_]+(\.[a-z0-9_]+)*$/;  // Chrome's native messaging host name rule
const quote = value => `'${String(value).replaceAll("'", `'\\''`)}'`;
export const RELAY = path.join(import.meta.dirname, 'control-relay.mjs');
const MAX_CONNECTIONS = 1000;
// How long a launched control host may go without its relay's start line before the run
// fails: the launcher's exec waits in open() on the FIFO until the relay opens it, so a
// relay that never starts leaves the host unstarted and the browser waiting.
export const RELAY_START_MS = 30000;

// Writes <manifestDirectory>/<name>.json (Chrome's NativeMessagingHosts entry) pointing at
// <launcherDirectory>/<name>.launcher. command is the host's argv; the browser's own
// arguments (the caller origin, a parent window) follow it. observe: an absolute directory
// for the control-pipe observation (above); node is the relay's interpreter.
export async function writeNativeHost({ manifestDirectory, launcherDirectory = manifestDirectory, pidDirectory,
  name, command, allowedOrigins, description = 'Cassini test host', observe = null, node = process.execPath }) {
  if (!NAME.test(name)) throw new Error(`invalid native host name ${name}`);
  if (!Array.isArray(command) || !command.length || !command.every(part => typeof part === 'string' && part))
    throw new Error('command is the host argv, a non-empty array of strings');
  const directories = [manifestDirectory, launcherDirectory, pidDirectory, ...(observe === null ? [] : [observe])];
  for (const directory of directories)
    if (typeof directory !== 'string' || !path.isAbsolute(directory)) throw new Error(`${directory}: an absolute directory`);
  await Promise.all(directories.map(d => mkdir(d, { recursive: true, mode: 0o700 })));
  const launcher = path.join(launcherDirectory, `${name}.launcher`);
  // name is a host name (letters, digits, _ and .), safe inside double quotes; the
  // directories are single-quoted. The pid file appears whole (rename), never half-written.
  const lines = [
    '#!/bin/sh',
    `# Written by cassini-browser-protocol scenarios/native-launcher.mjs: record this pid, ${observe === null ? '' : 'relay stdout, '}then exec the host.`,
    // Private files for the launcher's own records; the host gets the browser's umask back.
    'mask=$(umask)',
    'umask 077',
    `pids=${quote(pidDirectory)}`,
    `printf '%s\\n' "$$" > "$pids/.${name}-$$.tmp" || exit 70`,
    `mv -f "$pids/.${name}-$$.tmp" "$pids/${name}-$$.pid" || exit 70`,
  ];
  if (observe === null) lines.push('umask "$mask"', `exec ${command.map(quote).join(' ')} "$@"`);
  else lines.push(
    `base=${quote(observe)}`,
    'n=1',
    'until mkdir "$base/$n" 2>/dev/null; do',
    `  n=$((n + 1)); [ "$n" -le ${MAX_CONNECTIONS} ] || exit 70`,
    'done',
    'dir="$base/$n"',
    `printf '%s %s\\n' "$$" "$PPID" > "$dir/.host.tmp" || exit 70`,
    'mv -f "$dir/.host.tmp" "$dir/host" || exit 70',
    'mkfifo "$dir/out" || exit 70',
    // "$$" (the launcher, later the host): the FIFO's writer, which the relay watches until it opens.
    `( exec ${quote(node)} ${quote(RELAY)} "$dir/out" "$dir/frames.jsonl" "$$" </dev/null 2>"$dir/relay.err" & )`,
    'umask "$mask"',
    `exec ${command.map(quote).join(' ')} "$@" >"$dir/out"`,
  );
  await writeFile(launcher, [...lines, ''].join('\n'), { mode: 0o700 });
  await chmod(launcher, 0o700);
  const manifest = path.join(manifestDirectory, `${name}.json`);
  await writeFile(manifest, JSON.stringify({ name, description, path: launcher, type: 'stdio', allowed_origins: allowedOrigins }));
  return { manifest, launcher, pidDirectory, name, command, observe };
}

// A /proc read of a process that is exiting fails with ESRCH rather than ENOENT.
const gone = error => error.code === 'ENOENT' || error.code === 'ESRCH';

function alive(pid) {
  try { process.kill(pid, 0); } catch (error) { if (error.code === 'ESRCH') return false; if (error.code !== 'EPERM') throw error; }
  return true;
}

// A process's exact argv, or null once it is gone or a zombie: /proc on Linux; elsewhere
// (macOS) the kernel's KERN_PROCARGS2 through python3's ctypes, since ps joins the arguments
// with spaces (paths may hold spaces) and escapes control characters, so their boundaries
// cannot be read back. The driver already needs python3 (check.py).
const PROCARGS = `
import ctypes, ctypes.util, json, sys
libc = ctypes.CDLL(ctypes.util.find_library('c'), use_errno=True)
def argv(pid):
    mib = (ctypes.c_int * 3)(1, 49, pid)  # CTL_KERN, KERN_PROCARGS2: argc, exec path, argv
    size = ctypes.c_size_t(0)
    if libc.sysctl(mib, 3, None, ctypes.byref(size), None, 0) != 0: return None
    buffer = ctypes.create_string_buffer(size.value)
    if libc.sysctl(mib, 3, buffer, ctypes.byref(size), None, 0) != 0: return None
    raw = buffer.raw[:size.value]
    if len(raw) < 5: return None
    argc, at = int.from_bytes(raw[:4], sys.byteorder), raw.find(b'\\0', 4)
    if at < 0: return None
    while at < len(raw) and raw[at] == 0: at += 1
    parts = raw[at:].split(b'\\0')
    return [p.decode('utf-8', 'surrogateescape') for p in parts[:argc]] if 0 < argc <= len(parts) else None
print(json.dumps(argv(int(sys.argv[1]))))
`;
export async function processArgv(pid) {
  if (process.platform === 'linux') {
    try {
      const stat = await readFile(`/proc/${pid}/stat`, 'utf8');
      if (stat.slice(stat.lastIndexOf(')') + 2).startsWith('Z')) return null;
      const argv = (await readFile(`/proc/${pid}/cmdline`, 'utf8')).split('\0');
      return argv.at(-1) === '' ? argv.slice(0, -1) : argv;  // the kernel ends each argument with NUL
    } catch (error) { if (gone(error)) return null; throw error; }
  }
  return new Promise((resolve, reject) => execFile('python3', ['-I', '-c', PROCARGS, String(pid)], (error, stdout, stderr) =>
    error ? reject(new Error(`reading the argv of ${pid} needs python3: ${stderr || error.message}`)) : resolve(JSON.parse(stdout))));
}

// Whether argv runs command: command's arguments as one contiguous run, whole arguments only
// (a #! script's interpreter comes first, the browser's own arguments follow).
export function runsCommand(argv, command) {
  if (!Array.isArray(argv)) return false;
  for (let at = 0; at + command.length <= argv.length; at++)
    if (command.every((part, i) => argv[at + i] === part)) return true;
  return false;
}

// The process must still be the host the launcher exec'd: not gone, not a zombie, and running
// the host's whole command, so neither a recycled pid (another program, or the same
// interpreter with other arguments) nor the launcher still waiting to exec
// (/bin/sh <launcher>) is mistaken for it.
async function stillHost(pid, command) {
  return alive(pid) && runsCommand(await processArgv(pid), command);
}

// Every pid a launcher recorded for this host, oldest file order, with whether it is live.
export async function hostPids({ pidDirectory, name, command }) {
  const pattern = new RegExp(`^${name.replaceAll('.', '\\.')}-(\\d+)\\.pid$`);
  let entries;
  try { entries = await readdir(pidDirectory); }
  catch (error) { if (error.code === 'ENOENT') return []; throw error; }
  const pids = entries.map(entry => pattern.exec(entry)).filter(Boolean).map(match => Number(match[1]));
  return Promise.all(pids.sort((a, b) => a - b).map(async pid => ({ pid, live: await stillHost(pid, command) })));
}

export async function livePids(host) {
  return (await hostPids(host)).filter(entry => entry.live).map(entry => entry.pid);
}

// A process's parent pid: /proc on Linux, ps elsewhere; null once it is gone.
export async function parentPid(pid) {
  if (process.platform === 'linux') {
    try {
      const stat = await readFile(`/proc/${pid}/stat`, 'utf8');
      return Number(stat.slice(stat.lastIndexOf(')') + 2).split(' ')[1]);
    } catch (error) { if (gone(error)) return null; throw error; }
  }
  return new Promise(resolve => execFile('ps', ['-o', 'ppid=', '-p', String(pid)], (error, stdout) =>
    resolve(error ? null : Number(stdout.trim()))));
}

async function lines(file) {
  let text;
  try { text = await readFile(file, 'utf8'); } catch (error) { if (error.code === 'ENOENT') return []; throw error; }
  const complete = text.slice(0, text.lastIndexOf('\n') + 1);  // a line still being written waits
  return complete.split('\n').filter(Boolean).map(line => JSON.parse(line));
}

// Every control connection the observing launcher started, in launch order:
// {connection, pid, launchParent, launchedWall, live, parent, relay, frames, resumes,
// unparsable, ended, outputError, inputError}. pid, launchParent and launchedWall (when the
// launcher recorded them) are null until it has. parent is the pid's parent now, while it is
// still the host (else null). resumes is every resume request the relay read, each with
// delivered: false when the forward that carried it failed (outputError.undelivered): the
// host still asked, so it is still a request (scenario.json resumeObservation.request).
export async function controlConnections(host) {
  let entries;
  try { entries = await readdir(host.observe); }
  catch (error) { if (error.code === 'ENOENT') return []; throw error; }
  const numbers = entries.filter(entry => /^[1-9][0-9]*$/.test(entry)).map(Number).sort((a, b) => a - b);
  return Promise.all(numbers.map(async connection => {
    const directory = path.join(host.observe, String(connection));
    let pid = null, launchParent = null, launchedWall = null;
    try {
      const file = path.join(directory, 'host');
      [pid, launchParent] = (await readFile(file, 'utf8')).trim().split(' ').map(Number);
      launchedWall = (await stat(file)).mtimeMs;
    } catch (error) { if (error.code !== 'ENOENT') throw error; }
    const log = await lines(path.join(directory, 'frames.jsonl'));
    const live = pid !== null && await stillHost(pid, host.command);
    const outputError = log.find(entry => 'outputError' in entry) ?? null;
    const undelivered = new Set(outputError?.undelivered ?? []);
    return {
      connection, pid, launchParent, launchedWall, live, parent: live ? await parentPid(pid) : null,
      relay: log.find(entry => 'relay' in entry)?.relay ?? null,
      frames: log.filter(entry => 'frame' in entry),
      resumes: log.filter(entry => 'frame' in entry && 'resumeCapture' in entry)
        .map(entry => ({ ...entry, delivered: !undelivered.has(entry.frame) })),
      unparsable: log.find(entry => 'unparsable' in entry) ?? null,
      ended: log.some(entry => entry.end === true),
      outputError,
      inputError: log.find(entry => 'inputError' in entry) ?? null,
    };
  }));
}

// Whether pid is still a running control relay: not gone, not a zombie, and the relay script
// one of its arguments exactly (never a similarly named file such as control-relay.mjs.backup).
async function relayRunning(pid) {
  return alive(pid) && ((await processArgv(pid))?.includes(RELAY) ?? false);
}

async function relayErr(host, connection) {
  try { const text = (await readFile(path.join(host.observe, String(connection), 'relay.err'), 'utf8')).trim(); return text ? `; relay.err: ${text.slice(0, 500)}` : ''; }
  catch (error) { if (error.code === 'ENOENT') return ''; throw error; }
}

// Why one control connection, as read, fails the run (else null): a frame the relay could
// not read, an input error, a host that is not the browser's child (when launched, and now
// while it runs), or a relay that did not start within relayStartMs of the launch. A relay
// that is neither ended nor met the browser's closed end must still be running: that is
// checked by the caller, which reads the log again and applies this same function to it.
async function connectionFault(host, c, browserPid, label, relayStartMs, now) {
  if (c.unparsable) return `${label}: control connection ${c.connection} wrote a frame the relay cannot read: ${JSON.stringify(c.unparsable)}`;
  if (c.inputError) return `${label}: control connection ${c.connection}'s relay could not read the host's output: ${JSON.stringify(c.inputError)}`;
  // Every frame's evidence time is its monotonic stamp: a frame without one is never dropped silently.
  const unstamped = c.frames.find(frame => !stamped(frame));
  if (unstamped) return `${label}: ${unstampedFrame(c, unstamped)}`;
  if (c.pid === null) return null;  // the launcher is still starting it
  if (c.launchParent !== browserPid || (c.live && c.parent !== browserPid))
    return `${label}: control host ${c.pid} (connection ${c.connection}) is not the browser's child: ` +
      `launched by ${c.launchParent}, parent now ${c.parent}, browser ${browserPid}`;
  if (c.relay === null && now - c.launchedWall > relayStartMs)
    return `${label}: control connection ${c.connection}'s relay did not start within ${relayStartMs} ms of the launch, ` +
      `so its host waits on the FIFO and never runs${await relayErr(host, c.connection)}`;
  return null;
}
const copying = c => c.relay !== null && !c.ended && !c.outputError;

// The run fails unless every control host was the browser's child (when launched, and now
// while it runs), its relay started within relayStartMs and is still copying (or ended
// with its input, or met the browser's closed end), and every frame was readable. A relay
// found gone is judged on its log read again, by every check of the first read: it may
// have logged its end just before exiting, or an unreadable frame or input error.
// running(pid) is the liveness check (tests drive the interleaving through it).
// Returns the connections.
export async function verifyControlPipe(host, browserPid, label,
  { relayStartMs = RELAY_START_MS, now = Date.now(), running = relayRunning } = {}) {
  let connections = await controlConnections(host);
  for (const c of connections) {
    const fault = await connectionFault(host, c, browserPid, label, relayStartMs, now);
    if (fault) throw new Error(fault);
    if (!copying(c) || await running(c.relay)) continue;
    const again = (await controlConnections(host)).find(other => other.connection === c.connection);
    const refault = again ? await connectionFault(host, again, browserPid, label, relayStartMs, now) : `${label}: control connection ${c.connection} is gone`;
    if (refault) throw new Error(refault);
    if (copying(again))
      throw new Error(`${label}: control connection ${c.connection}'s relay ${c.relay} stopped before the end of its input` +
        await relayErr(host, c.connection));
    connections = connections.map(other => (other.connection === c.connection ? again : other));
  }
  return connections;
}

// The resumeRequests evidence (scenario.json resumeObservation.fields): every resume request
// read up to untilMono, in pipe order (relay time, then launch order and frame order), each
// with delivered (false: the relay's forward of it to the browser failed).
// Times are the relay's monotonic stamps (atMono) on the session timeline: originMono is the
// driver's session clock origin on the same clock (scenarios/session-clock.mjs).
// lostAtMs is the frame's resumeCapture.lostAtMs as written, null when absent (R94: the atMs
// of the committed seam the host asks to resume, not compared with the request's atMs). Unlike a missing stamp it never stops the run: a request without a finite lostAtMs is
// still a request (the host asked), and the oracle fails it (check.py O3.resumeAt).
export function resumeRequests(connections, originMono, untilMono) {
  // Every stamp is validated before any filtering or sorting: a missing one would drop the request
  // (undefined <= n is false) and a null one would place it at time zero.
  for (const c of connections) {
    const unstamped = c.resumes.find(r => !stamped(r));
    if (unstamped) throw new Error(unstampedFrame(c, unstamped));
  }
  return connections.flatMap(c => c.resumes.filter(r => r.atMono <= untilMono).map(r => ({ c, r })))
    .sort((x, y) => x.r.atMono - y.r.atMono || x.c.connection - y.c.connection || x.r.frame - y.r.frame)
    .map(({ c, r }) => ({ atMs: r.atMono - originMono, via: 'control-pipe', connection: c.connection, requestId: r.requestId,
      captureId: r.resumeCapture.captureId, timelineOriginMs: r.resumeCapture.timelineOriginMs,
      lostAtMs: r.resumeCapture.lostAtMs ?? null, delivered: r.delivered !== false }));
}

// A logged frame's evidence time is its monotonic stamp (scenarios/session-clock.mjs): a finite
// number, never missing, null or a string.
const stamped = frame => typeof frame.atMono === 'number' && Number.isFinite(frame.atMono);
const unstampedFrame = (c, frame) => `control connection ${c.connection}'s frame ${frame.frame} has no monotonic stamp ` +
  `(atMono ${'atMono' in frame ? JSON.stringify(frame.atMono) : 'missing'})`;

// Ends relays still running (their host gone and input not closed, or a launch cut short).
// Returns the pids it signalled.
export async function stopRelays(host) {
  const stopped = [];
  for (const c of await controlConnections(host)) {
    if (c.relay === null || c.ended || c.outputError) continue;
    // Only a pid that is still the relay: never a process that reused its pid.
    if (!await relayRunning(c.relay)) continue;
    try { process.kill(c.relay, 'SIGTERM'); stopped.push(c.relay); } catch (error) { if (error.code !== 'ESRCH') throw error; }
  }
  return stopped;
}

// Ends the launchers that never became their host: a launcher whose relay never opened the
// FIFO waits in open() for good, and the browser closing its pipes does not end it (it reads
// neither). Such a pid still runs the launcher script, not the host it would have exec'd:
// its argv decides: the launcher path as one whole argument (`live` excludes it as the host). A relay that had not started yet sees
// its writer gone and ends by itself (scenarios/control-relay.mjs). Returns the pids it killed.
export async function stopLaunchers(host) {
  if (!host?.launcher) return [];
  const killed = [];
  for (const c of await controlConnections(host)) {
    if (c.pid === null || !alive(c.pid)) continue;
    if (!((await processArgv(c.pid))?.includes(host.launcher) ?? false)) continue;
    try { process.kill(c.pid, 'SIGKILL'); killed.push(c.pid); } catch (error) { if (error.code !== 'ESRCH') throw error; }
  }
  return killed;
}
