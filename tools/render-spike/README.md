# Shared renderer spike inputs and metrics

Temporary prototype tools, not installed application code. Generate before measuring open:

```
python3 tools/render-spike/fixtures.py /your/private/shared/fixture-60 --minutes 60
python3 tools/render-spike/fixtures.py /your/private/shared/fixture-240 --minutes 240
python3 tools/render-spike/fixtures.py /your/private/shared/fixture-plain --minutes 60 --unassigned
```

Import `FrameProbe` from `probe.py` in your experiment process. Construct it before opening/attaching the renderer, passing a `Gtk.ScrolledWindow`, `anchor()` callback returning `(logical row or character position, within-item vertical pixel offset)` (offset increases going down), and `loaded()` (all content built). It records both paint thread CPU and interval thread CPU (the latter includes idle chunks), diagnostic open/ready duration, baseline/current/maximum RSS, adjustment upper/value and received input direction. It never handles input. Use `.phase` to label opening/paused/playback/resize. Wrap your position update with `probe.measure("highlight", callback, ...)`. Save via `probe.dump(path, ...)`. Do not assert wall time; event gates establish completion.

Evaluate `flick.js` via GNOME Shell Eval in your OWN desktop. It injects four downward and two upward native finger flicks at (900,450); adapt coordinates to your transcript pane. Native kinetics remains enabled. The shell writes `global._renderSpikeInput` and sets `_renderSpikeDone` after the six input sequences (this is input completion, not coast completion). Observe rendered anchors until momentum/adjustment events settle, using frame/input completion events, not a fixed sleep. Pause playback and then repeat with playback running/follow off. Do not connect a custom `decelerate` handler that changes scrolling.

Generated named fixture: two speakers, many short/long turns, inline acknowledgements and competing speech. Unassigned fixture: no named roster; words retain a required placeholder speaker id. Report actual model words/display rows/chips from your app's D84 pipeline rather than assuming the source turn count. Numeric turn prefixes make text unique so caches cannot gain artificial reuse from identical paragraphs.
