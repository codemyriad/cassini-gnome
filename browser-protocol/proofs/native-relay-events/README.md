# Native relay child lifetime

Mac CI failed in the real 70-source integration fixture with
[write after end](https://github.com/codemyriad/cassini-mac/actions/runs/43074974020/job/130719040723).
The fixture gave its Python child a 10-second total lifetime, spanning startup,
all source/track requests and Stop. A healthy but loaded writer could be killed
while acknowledgments were still progressing. Cleanup ended stdin while queued
relay work could still post, producing an uncaught stream error.

The child no longer has an overall lifetime deadline. Command results and page
acknowledgments resolve promises from the real VM port events; each missing
completion and the final process exit have a failure-only 10-second guard.
Closed fixture ports throw like Chrome's native port; stdin errors disconnect
and must remain absent at successful completion. Failed cleanup kills only the
owned child. No production timeout or writer behavior changes.

`manual-lifetime.cjs` is a diagnostic-only clock adapter: intercept the old
spawn lifetime option and expire it at the first actual native response, without
sleeping. It changes no framed packet or child response. Against the old test,
the real exchange fails; against the new test, all 70 source/64 track/Stop and
provenance assertions complete. Run from this checkout with:

```
node --require ./proofs/native-relay-events/manual-lifetime.cjs --test extension/tests/multistream-relay.test.cjs
```

A separate real production mutation drops labelSource in background.js's
stampDocument destructuring. The integration test fails its browser-versus-dom
assertion. Restore production before running the full Node unit partition.
Logs are retained on the coordinator at ~/coordination/evidence/mac-flaky/:
protocol-relay-before-deadline.log, protocol-relay-after-deadline.log,
protocol-relay-mutation.log and protocol-relay-all.log. Every success depends
on actual protocol events, never the passage of time.
