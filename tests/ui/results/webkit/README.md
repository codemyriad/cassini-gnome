WebKit rendering spike evidence, synthetic English only.

`summary.json` and compressed per-frame traces are from the `dom-smooth` geometry audit: five instant pixel steps followed by the browser's native smooth-scroll animation. They do **not** certify touchpad kinetics. `nativeKineticCertified` is false. Each case includes six down/up sequences paused and six while playing with follow disabled.

`mutter-diagnostics.json` records the failed delivery of native synthetic finger input. Its stationary sequences are not counted as successful zero-reversal tests.

PNG files are real WebKit visible-region snapshots, taken after layout readiness. `accessibility.json` contains the bounded native AT-SPI tree through the first seek button. `environment.json` records library and desktop versions. To inspect traces: `gzip -dc hour-frames.json.gz | python3 -m json.tool`.

See [the report](../../../../docs/spikes/render-webkit.md) for methods, limitations, packaging and reproduction.
