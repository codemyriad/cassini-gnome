# Browser recording contract

The canonical specification, schemas, extension and conformance vectors live in
[codemyriad/cassini-browser-protocol](https://github.com/codemyriad/cassini-browser-protocol).
This app vendors commit
[`0ccec2351f591417ac800b0157555e0d6c3a085a`](https://github.com/codemyriad/cassini-browser-protocol/tree/0ccec2351f591417ac800b0157555e0d6c3a085a)
in [`browser-protocol/`](../browser-protocol/). See its [SPEC.md](../browser-protocol/SPEC.md).

`PINNED_COMMIT` and the original `UPSTREAM_COMMIT` object allow an offline Git
object/tree verification of the entire upstream snapshot. The upstream
`verify.py` remains unmodified and additionally checks the extension copies:

```sh
python3 build-aux/verify-browser-protocol.py
```

Protocol and extension changes land upstream first. Update the pin, re-vendor
the Git archive and original commit object, then copy `extension/` byte-for-byte.
Do not patch shared files in this app. GNOME-specific packaging, launchers and
instructions remain outside the shared extension file set.
