# Browser recording protocol

The canonical contract, schemas, shared extension and conformance vectors are
vendored under [browser-protocol](../browser-protocol/README.md). Its exact Git
revision is recorded in [PINNED_COMMIT](../browser-protocol/PINNED_COMMIT).
The current pin, `6e581d66`, ships companion 0.2.3 with authoritative page-state
publication and a coherent multi-tab inventory. D33 records explicit end causes
and permits `othersSource.reason: restored` only for a retained browser connection
returning with fresh PCM after fallback; older recordings remain readable.

Run `python3 browser-protocol/verify.py --pinned browser-extension` to verify
both the Git snapshot and extension bytes. Update with
`build-aux/vendor-browser-protocol.sh /path/to/upstream FULL_COMMIT` after the
shared change has landed upstream. Install `browser-protocol/requirements-test.txt`
to run the request-derived stdio conformance suite.

## Browser call lifetime

For independent captures, a reachable page reporting zero call sources ends the
meeting after a two-second grace; it does not select system sound. Losing the
page or companion invokes Q4. A fallback keeps its existing folder and sources
when an active call returns, without switching back to browser audio. If no
active call returns within 30 seconds, Cassini stops and saves it, including when
the browser connection itself is gone. The broker advances these deadlines on
busy accept traffic as well as idle timeouts.

The private recording harness proves hang-up with a tab left open, reload,
worker reconnection, expiry without an active call, and unaffected simultaneous
meetings. Unit tests advance only the recorded missing timestamp to exercise the
same deadline branches without wall-clock sleeps.
