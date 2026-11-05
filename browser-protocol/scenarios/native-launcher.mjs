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
    `( exec ${quote(node)} ${quote(RELAY)} "$dir/out" "$dir/frames.jsonl" </dev/null 2>"$dir/relay.err" & )`,
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

// On Linux the process must still be the host the launcher exec'd (not a zombie, its argv
// holding the command in order: a #! script's interpreter comes first), so a recycled pid
// is never mistaken for it. Elsewhere liveness is kill(pid, 0).
async function stillHost(pid, command) {
  if (!alive(pid)) return false;
  let stat, argv;
  try {
    stat = await readFile(`/proc/${pid}/stat`, 'utf8');
    argv = (await readFile(`/proc/${pid}/cmdline`, 'utf8')).split('\0');
  } catch (error) {
    if (gone(error) && process.platform !== 'linux') return true;
    if (gone(error)) return false;
    throw error;
  }
  if (stat.slice(stat.lastIndexOf(')') + 2).startsWith('Z')) return false;
  let at = 0;
  for (const part of argv) if (part === command[at]) at++;
  return at === command.length;
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
// still the host (else null). resumes leaves out frames a failed forward never delivered.
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
      resumes: log.filter(entry => 'frame' in entry && 'resumeCapture' in entry && !undelivered.has(entry.frame)),
      unparsable: log.find(entry => 'unparsable' in entry) ?? null,
      ended: log.some(entry => entry.end === true),
      outputError,
      inputError: log.find(entry => 'inputError' in entry) ?? null,
    };
  }));
}

// Whether pid is still a running control relay (not gone, not a zombie, its argv the relay).
async function relayRunning(pid) {
  if (!alive(pid)) return false;
  try {
    const stat = await readFile(`/proc/${pid}/stat`, 'utf8');
    if (stat.slice(stat.lastIndexOf(')') + 2).startsWith('Z')) return false;
    return (await readFile(`/proc/${pid}/cmdline`, 'utf8')).split('\0').includes(RELAY);
  } catch (error) {
    if (gone(error)) return process.platform !== 'linux';
    throw error;
  }
}

async function relayErr(host, connection) {
  try { const text = (await readFile(path.join(host.observe, String(connection), 'relay.err'), 'utf8')).trim(); return text ? `; relay.err: ${text.slice(0, 500)}` : ''; }
  catch (error) { if (error.code === 'ENOENT') return ''; throw error; }
}

// The run fails unless every control host was the browser's child (when launched, and now
// while it runs), its relay started within relayStartMs and is still copying (or ended
// with its input, or met the browser's closed end), and every frame was readable.
// Returns the connections.
export async function verifyControlPipe(host, browserPid, label, { relayStartMs = RELAY_START_MS, now = Date.now() } = {}) {
  let connections = await controlConnections(host);
  for (const c of connections) {
    if (c.unparsable) throw new Error(`${label}: control connection ${c.connection} wrote a frame the relay cannot read: ${JSON.stringify(c.unparsable)}`);
    if (c.inputError) throw new Error(`${label}: control connection ${c.connection}'s relay could not read the host's output: ${JSON.stringify(c.inputError)}`);
    if (c.pid === null) continue;  // the launcher is still starting it
    if (c.launchParent !== browserPid || (c.live && c.parent !== browserPid))
      throw new Error(`${label}: control host ${c.pid} (connection ${c.connection}) is not the browser's child: ` +
        `launched by ${c.launchParent}, parent now ${c.parent}, browser ${browserPid}`);
    if (c.relay === null && now - c.launchedWall > relayStartMs)
      throw new Error(`${label}: control connection ${c.connection}'s relay did not start within ${relayStartMs} ms of the launch, ` +
        `so its host waits on the FIFO and never runs${await relayErr(host, c.connection)}`);
    if (c.relay !== null && !c.ended && !c.outputError && !await relayRunning(c.relay)) {
      // It may have logged its end just before exiting: read the log again before failing.
      const again = (await controlConnections(host)).find(other => other.connection === c.connection);
      if (!again.ended && !again.outputError && !again.inputError && !again.unparsable)
        throw new Error(`${label}: control connection ${c.connection}'s relay ${c.relay} stopped before the end of its input` +
          await relayErr(host, c.connection));
      connections = connections.map(other => (other.connection === c.connection ? again : other));
    }
  }
  return connections;
}

// The resumeRequests evidence (scenario.json resumeObservation.fields): every resume request
// read up to untilWall, in pipe order (relay time, then launch order and frame order).
export function resumeRequests(connections, origin, untilWall) {
  return connections.flatMap(c => c.resumes.filter(r => r.atWall <= untilWall).map(r => ({ c, r })))
    .sort((x, y) => x.r.atWall - y.r.atWall || x.c.connection - y.c.connection || x.r.frame - y.r.frame)
    .map(({ c, r }) => ({ atMs: r.atWall - origin, via: 'control-pipe', connection: c.connection, requestId: r.requestId,
      captureId: r.resumeCapture.captureId, timelineOriginMs: r.resumeCapture.timelineOriginMs }));
}

// Ends relays still running (their host gone and input not closed, or a launch cut short).
// Returns the pids it signalled.
export async function stopRelays(host) {
  const stopped = [];
  for (const c of await controlConnections(host)) {
    if (c.relay === null || c.ended || c.outputError) continue;
    try {
      const argv = (await readFile(`/proc/${c.relay}/cmdline`, 'utf8')).split('\0');
      if (!argv.includes(RELAY)) continue;
    } catch (error) { if (gone(error) && process.platform === 'linux') continue; if (!gone(error)) throw error; }
    try { process.kill(c.relay, 'SIGTERM'); stopped.push(c.relay); } catch (error) { if (error.code !== 'ESRCH') throw error; }
  }
  return stopped;
}
