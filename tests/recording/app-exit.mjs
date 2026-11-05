// Ending the recording harness's child processes within a bound (tests/recording/live-folder.mjs).

// A child's exit, or false once ms pass (a bound on that process's own exit, not a wait).
export const exitWithin = (child, ms) => child.exitCode !== null || child.signalCode !== null ? Promise.resolve(true)
 : new Promise(resolve => {const timer = setTimeout(() => resolve(false), ms); child.once('exit', () => {clearTimeout(timer); resolve(true);});});

// Quits the app: ask() sends the quit request, then its stdin ends. Whatever the request did
// (a wedged app answers nothing, so the request times out), an app that has not exited
// within ms is killed. Returns 'done' or 'killed', with the request's error when it failed.
export async function quitApp(ui, ask, ms = 10000) {
 const exited = exitWithin(ui, ms);
 let failure = null;
 try {await ask();} catch (error) {failure = error;} finally {ui.stdin.end();}
 let outcome = 'done';
 if (!await exited) {ui.kill('SIGKILL'); outcome = 'killed';}
 return failure ? `${outcome} (quit request: ${failure.message || failure})` : outcome;
}
