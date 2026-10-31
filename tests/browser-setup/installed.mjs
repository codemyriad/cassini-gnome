// SPDX-License-Identifier: GPL-3.0-or-later
// A genuine installed package, fresh profile and real extension; no Connect action.
import assert from 'node:assert/strict';
import {spawn, execFile} from 'node:child_process';
import {promisify} from 'node:util';
import {createHash} from 'node:crypto';
import {openSync, closeSync} from 'node:fs';
import {mkdir, mkdtemp, readFile, writeFile, chmod, stat, rm} from 'node:fs/promises';
import path from 'node:path';
import {chromium} from '../recording/node_modules/playwright/index.mjs';

const execute = promisify(execFile), output = path.resolve(process.argv[2]);
await mkdir(output, {recursive: true});
const root = await mkdtemp(path.join(output, 'fresh-'));
const home = path.join(root, 'home'), config = path.join(root, 'config');
const env = {...process.env, HOME: home, XDG_CONFIG_HOME: config,
 XDG_DATA_HOME: path.join(root, 'data'), XDG_CACHE_HOME: path.join(root, 'cache'),
 XDG_RUNTIME_DIR: path.join(root, 'run'), CASSINI_LIBRARY_DIR: path.join(root, 'Library'),
 PATH: path.join(root, 'bin') + ':/opt/node/bin:/usr/bin:/bin'};
for (const folder of [home, config, env.XDG_DATA_HOME, env.XDG_CACHE_HOME, env.XDG_RUNTIME_DIR,
 env.CASSINI_LIBRARY_DIR, path.join(root, 'bin')]) await mkdir(folder, {recursive: true, mode: 0o700});
const quote = value => "'" + value.replaceAll("'", "'\\''") + "'";
const wrapper = path.join(root, 'bin/chromium');
await writeFile(wrapper, '#!/bin/sh\nexec ' + quote(chromium.executablePath()) + ' "$@"\n');
await chmod(wrapper, 0o700);
const profile = path.join(config, 'chromium');
const extension = '/usr/share/cassini-viewer/browser-extension';
const names = ['io.codemyriad.cassini', 'io.codemyriad.cassini.control'];
const files = names.map(name => path.join(profile, 'NativeMessagingHosts', name + '.json'));
const log = openSync(path.join(output, 'application.log'), 'w', 0o600);
let app, appError, context, status;
const report = {noUserAction: true, freshProfile: true};
async function poll(check, label, milliseconds = 20000) {
 const deadline = Date.now() + milliseconds;
 let last;
 while (Date.now() < deadline) {
  try {const value = await check(); if (value) return value;} catch (error) {last = error;}
  if (appError) throw appError;
  if (app && (app.exitCode !== null || app.signalCode !== null)) throw new Error('Application exited early: ' + (app.exitCode ?? app.signalCode));
  await new Promise(resolve => setTimeout(resolve, 50));
 }
 throw new Error(label + (last ? ': ' + last.message : ''));
}
try {
 assert.equal((await execute('dpkg', ['--verify', 'cassini-talk'])).stdout.trim(), '', 'installed package was modified');
 report.package = (await execute('dpkg-query', ['-W', '-f=${Version}', 'cassini-talk'])).stdout;
 await assert.rejects(stat(profile), {code: 'ENOENT'});
 app = spawn('/usr/bin/cassini-talk', [], {env, stdio: ['ignore', log, log]});
 app.on('error', error => {appError = error;});
 await poll(async () => (await Promise.all(files.map(file => stat(file)))).length === 2, 'automatic host registration');
 const manifests = await Promise.all(files.map(async file => JSON.parse(await readFile(file, 'utf8'))));
 for (let i = 0; i < names.length; i++) {
  assert.equal(manifests[i].name, names[i]);
  assert.equal(manifests[i].type, 'stdio');
  assert.equal(manifests[i].path, '/usr/bin/cassini-browser-' + (i ? 'control-host' : 'host'));
  assert.deepEqual(manifests[i].allowed_origins, ['chrome-extension://pcafdaahfhjiolfpfnlhmljfgoabbaij/']);
 }
 const before = await Promise.all(files.map(async file => {const s = await stat(file, {bigint: true}); return [s.ino, s.mtimeNs];}));
 const manifest = JSON.parse(await readFile(path.join(extension, 'manifest.json'), 'utf8'));
 const id = [...createHash('sha256').update(Buffer.from(manifest.key, 'base64')).digest('hex').slice(0, 32)]
  .map(c => String.fromCharCode(97 + parseInt(c, 16))).join('');
 assert.equal(id, 'pcafdaahfhjiolfpfnlhmljfgoabbaij');
 context = await chromium.launchPersistentContext(profile, {headless: false, executablePath: chromium.executablePath(), env,
  args: ['--no-sandbox', '--disable-dev-shm-usage', '--disable-extensions-except=' + extension, '--load-extension=' + extension]});
 const worker = context.serviceWorkers()[0] || await context.waitForEvent('serviceworker', {timeout: 15000});
 assert.equal(await worker.evaluate(() => chrome.runtime.id), id);
 await poll(async () => {
  const value = await execute('python3', ['-c', 'import json;from cassini_viewer.browser_service import request;print(json.dumps(request()))'],
   {env: {...env, PYTHONPATH: '/usr/share/cassini-viewer'}, timeout: 4000});
  status = JSON.parse(value.stdout);
  return status.connectedBrowsers >= 1;
 }, 'real extension hello without Connect');
 const after = await Promise.all(files.map(async file => {const s = await stat(file, {bigint: true}); return [s.ino, s.mtimeNs];}));
 assert.deepEqual(after, before, 'identical registrations were rewritten');
 report.extensionId = id;
 report.connectedBrowsers = status.connectedBrowsers;
 report.hosts = names;
 report.manifestsUnchanged = true;
 await context.close(); context = null;
 await execute('gapplication', ['action', 'io.codemyriad.CassiniTalk', 'quit'], {env, timeout: 4000});
 const deadline = Date.now() + 5000;
 while (app.exitCode === null && app.signalCode === null && Date.now() < deadline) await new Promise(resolve => setTimeout(resolve, 20));
 assert.equal(app.exitCode, 0, 'application did not quit promptly');
 report.cleanExit = true;
 await writeFile(path.join(output, 'startup.json'), JSON.stringify(report, null, 2) + '\n');
 console.log('PASS installed browser setup: fresh profile, both hosts, real extension auto hello, unchanged manifests, clean exit');
} catch (error) {
 await writeFile(path.join(output, 'failure.json'), JSON.stringify({error: error.stack, status}, null, 2) + '\n');
 throw error;
} finally {
 if (context) await context.close();
 if (app && app.exitCode === null) app.kill('SIGKILL');
 closeSync(log);
 await rm(root, {recursive: true, force: true});
}
