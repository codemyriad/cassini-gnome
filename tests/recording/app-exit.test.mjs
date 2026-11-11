import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {once} from 'node:events';
import {test} from 'node:test';
import {exitWithin, quitApp} from './app-exit.mjs';

const BOUND = 10000; // bound on each wait below; every wait ends on its event
const exitOf = child => new Promise(resolve => child.exitCode !== null || child.signalCode !== null
 ? resolve({code: child.exitCode, signal: child.signalCode}) : child.once('exit', (code, signal) => resolve({code, signal})));
// An app whose main loop is stuck: it reads nothing and answers nothing, and an ended stdin
// does not end it.
async function wedged(t) {
 const ui = spawn(process.execPath, ['-e', 'process.on("message", () => {}); process.send("ready")'],
  {stdio: ['pipe', 'pipe', 'ignore', 'ipc']});
 t.after(() => ui.kill('SIGKILL'));
 const [message] = await once(ui, 'message');
 assert.equal(message, 'ready');
 return ui;
}

test('a wedged app whose quit request is never answered is still killed at the bound', {timeout: BOUND}, async t => {
 const ui = await wedged(t);
 t.mock.timers.enable({apis: ['setTimeout']});
 let rejectRequest;
 const unanswered = () => new Promise((_, reject) => {rejectRequest = reject;});
 const quitting = quitApp(ui, unanswered, 200);
 rejectRequest(new Error('GNOME UI request timed out'));
 t.mock.timers.tick(200);
 const outcome = await quitting;
 assert.match(String(outcome), /^killed/);
 assert.deepEqual(await exitOf(ui), {code: null, signal: 'SIGKILL'});
 assert.equal(ui.stdin.writableEnded, true, 'stdin ended whatever the request did');
});

test('an app that answers the quit request and exits is not killed', {timeout: BOUND}, async () => {
 const ui = spawn(process.execPath, ['-e', 'process.stdin.resume(); process.stdin.on("end", () => process.exit(0))'], {stdio: ['pipe', 'pipe', 'ignore']});
 const outcome = await quitApp(ui, async () => ({}), BOUND);
 assert.doesNotMatch(String(outcome), /killed/);
 assert.deepEqual(await exitOf(ui), {code: 0, signal: null});
});

test('exitWithin is true at once for an exited child and false at its bound otherwise', {timeout: BOUND}, async t => {
 const ui = await wedged(t);
 t.mock.timers.enable({apis: ['setTimeout']});
 const expired = exitWithin(ui, 20);
 t.mock.timers.tick(20);
 assert.equal(await expired, false);
 ui.kill('SIGKILL');
 await exitOf(ui);
 assert.equal(await exitWithin(ui, BOUND), true);
});
