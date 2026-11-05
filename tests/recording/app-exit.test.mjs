import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {test} from 'node:test';
import {exitWithin, quitApp} from './app-exit.mjs';

const BOUND = 10000; // bound on each wait below; every wait ends on its event
const exitOf = child => new Promise(resolve => child.exitCode !== null || child.signalCode !== null
 ? resolve({code: child.exitCode, signal: child.signalCode}) : child.once('exit', (code, signal) => resolve({code, signal})));
// An app whose main loop is stuck: it reads nothing and answers nothing, and an ended stdin
// does not end it.
const wedged = () => spawn(process.execPath, ['-e', 'setInterval(() => {}, 1 << 30)'], {stdio: ['pipe', 'pipe', 'ignore']});

test('a wedged app whose quit request is never answered is still killed at the bound', async t => {
 const ui = wedged();
 t.after(() => ui.kill('SIGKILL'));
 // The UI request times out (call() rejects after its own bound) and the app never exits.
 const unanswered = () => new Promise((_, reject) => setTimeout(() => reject(new Error('GNOME UI request timed out')), 50));
 const outcome = await quitApp(ui, unanswered, 200);
 assert.match(String(outcome), /^killed/);
 assert.deepEqual(await exitOf(ui), {code: null, signal: 'SIGKILL'});
 assert.equal(ui.stdin.writableEnded, true, 'stdin ended whatever the request did');
});

test('an app that answers the quit request and exits is not killed', async () => {
 const ui = spawn(process.execPath, ['-e', 'process.stdin.resume(); process.stdin.on("end", () => process.exit(0))'], {stdio: ['pipe', 'pipe', 'ignore']});
 const outcome = await quitApp(ui, async () => ({}), BOUND);
 assert.doesNotMatch(String(outcome), /killed/);
 assert.deepEqual(await exitOf(ui), {code: 0, signal: null});
});

test('exitWithin is true at once for an exited child and false at its bound otherwise', async t => {
 const ui = wedged();
 t.after(() => ui.kill('SIGKILL'));
 assert.equal(await exitWithin(ui, 20), false);
 ui.kill('SIGKILL');
 await exitOf(ui);
 assert.equal(await exitWithin(ui, BOUND), true);
});
