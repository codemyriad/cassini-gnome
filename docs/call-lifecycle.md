# Browser call lifetime (D33)

Shared protocol pin: `a6d240e83ca6242e807d9bf1140838e113817da7` (merged PR #24).
Companion 0.2.4 replaces older local installs; no Chrome Web Store packaging or upload is performed.

The broker owns every independent capture and its Library folder. With the
`page-state-v1` capability it starts the extension with `keepOnNavigation: true`.
The original capture ID, native connection and clock survive navigation. Older
extensions retain their existing transport behavior.

For independent sessions, a top-level status with a tab ID and no `sessions`
list updates only that tab. Older single-session status keeps its global meaning.
It retains the known capture ID, path and stopping state when omitted; explicit
fields take priority, and a replacement capture inherits none of its predecessor's
state. This also covers the interval before the audio socket opens and connections
without a browser instance. Every shared wire case runs both as published and
with tab-scoped status deltas, checking retained folder/microphone ownership and
the actual saved end reason.

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

A control-worker reconnect has its own bounded 10-second rediscovery window
(D43.1). It is keyed by the original browser instance and tab, and preserves the
existing capture only while fresh PCM continues on its retained audio connection.
A replacement worker may report status before its tab inventory; it adopts only
the tabs it actually reports. Another profile or browser cannot extend the window,
and retiring an overlapping old worker cannot detach the replacement. A stalled
or closed audio connection still triggers the ordinary fallback, and an explicit
closed tab or ended call keeps its 2-second policy. The 120-second return deadline
starts at the original control loss and is never reset by a half-reconnect.
Recent PCM remains fresh across the instant of control disconnect; the next
buffer need not have arrived yet. A reported navigation invalidates the old
document's positive source count. A fast returning status waits for fresh
inventory evidence before clearing the return timer.

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

D43.1(a) separates a call's public identity from its transport. With a browser
instance, its opaque `call/…` ID is derived from that instance, tab and URL, so a
worker reconnect keeps the same ID and a different address is a different call.
Transport UUIDs stay immutable. Commands and queued automatic/fallback work resolve
the logical call to the newest matching connection at dispatch; partial inventory
gets the bounded reconnect window. An accepted Start whose reply was lost waits
for replacement status instead of sending Start again. An unresolved Start is
only retried after that window, and Stop still prevents automatic restarts.
Overlapping transports publish one call; stale navigation or disconnect cannot
clear the replacement's remembered Stop. Disconnected aliases and lineage are
bounded, and browsers without an instance keep their existing transport IDs.

D43.1(c) gives Record-all and Stop-all one 25-second broker budget, inside the
application socket's 30-second timeout. Independent calls run concurrently on a
bounded batch executor; a slow tab cannot serialize every other acknowledgement.
The same deadline covers command waits, reconnect recovery and fallback startup.
Each unfinished Start is reported as a failure; queued Starts are cancelled and
fallbacks that finish too late are stopped and kept. Already accepted successful
recordings keep running. Start and Stop share the transport write lock so Stop
cannot overtake a prepared Start and leave it recording afterwards.

Stop schedules native saves before browser acknowledgement work and remembers
all owned calls immediately. If saving outlasts the request, it continues in the
broker and the start barrier stays set until every background Stop finishes.
Overlapping Stop batches each retain their own barrier token. A missing browser
acknowledgement is ignored only after its recording has actually stopped; a live
unresponsive recording or failed native save remains an error. No audio sample,
tone, bleed or source-lifecycle thresholds are changed.

Shared wire vectors also exercise immediate page-state statuses around delayed inventories. Navigation invalidates the departing document’s source count; only fresh inventory evidence clears the return deadline. The companion waits for the entire metadata batch before reading all rows’ current source evidence.

Attempt suppression belongs to the logical call as well as its current transport.
Heard loss, address changes, expired remembered stops and finished capture owners
clear both together. A stale worker cannot clear its replacement's attempt.
Manual Record rejects a Start still pending on an overlapping transport.

A timed-out batch returns on its request deadline while cleanup gets a fresh,
bounded command wait. The broker retains a bounded correlation history so late
results update their own tab; an obsolete Stop cannot hide a newer Start. Audio
announced after cancellation is saved and retired, even if its capture identity
arrives after Stop was dispatched. A returning worker that reports a stopped
call receives Stop. Stop-all deduplicates logical calls, and Quit joins an
existing Stop instead of dispatching a competing command.
