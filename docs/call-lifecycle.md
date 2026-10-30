# Browser call lifetime (D33)

Shared protocol pin: `bb13da92fa8c3192bb6d9b8444acd3197c772d62` (merged PR #22).
The extension stays at 0.2.2; this change does not package or upload it.

The broker owns every independent capture and its Library folder. With the
`page-state-v1` capability it starts the extension with `keepOnNavigation: true`.
The original capture ID, native connection and clock survive navigation. Older
extensions retain their existing transport behavior.

Each capture has its own monotonic timers. A present page with no sources stops
after 2 seconds (`callEnded`). A browser-confirmed closed tab stops after 2 seconds
(`tabClosed`) without starting computer-sound capture. Navigation or an absent
page allows 120 seconds for return; after the initial 2 seconds Cassini uses
computer sound if The others is selected. The stored deadline is checked before
accepting a delayed return; expiry stops with `pageGone`.

A present, same-tab page with audio can restore the browser only on its retained
live audio connection. Fresh validated PCM makes the source ready; an old track
declaration does not. The source event (`browser`, `restored`), new browser spans
and ended system span share a checkpoint. Only actual returning wire sources
get spans. A later source starts a span on its first PCM. The microphone and
folder remain unchanged, and actual opaque browser track IDs preserve speaker
identity where available. Device/limit/sync failure keeps the previous selection
and source. A post-rename durability failure attempts to restore the previous
metadata and retains every proposed WAV, including when recovery cannot sync it.
A dead native connection (for example a browser worker restart) is not presented
as a restored source; its working system fallback stays selected.

Every actual stop writes one terminal session event: `user`, `callEnded`,
`tabClosed`, `pageGone`, `disk`, `error` or `appQuit`. Accepted PCM is finalized
before the event and saved state are checkpointed. Its timestamp is at least
every known track end and prior event, on the original session clock. Repeated
Stop cannot rewrite it. Earlier PCM-gap warnings do not replace an explicit Stop
cause. New source changes reserve the final slot of the 1000-event budget.
Older sessions without an end remain readable and do not acquire an inferred
reason. Recording details reads this saved evidence.

Tests run the shared literal lifecycle vectors through the real broker, device
substitute and session writer. Other tests inject source and durability failures,
retain source identity across spans, exercise real native EOS, and verify WAV
bytes/track endpoints. The private browser integration loads the actual extension
and app with private Pulse, Xvfb and D-Bus: hang-up, tab closure, reload and restored
browser tone, independent microphone/other call, worker reconnect and real 120 s
expiry. Existing sample-count, tone and bleed thresholds are unchanged.
