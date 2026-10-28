# S-6 source events fixture

CC0-1.0 synthetic silent PCM and metadata. `session.json` is version 1, with
the optional S-6 events. `expected.json` is written by hand from SESSION-EVENTS.md.

Run `python3 make.py /temporary/Library` to create a real folder with sparse
PCM16 WAVs. No speech or personal recording is used. Silence is deliberate;
this fixture checks timeline metadata and reading, not recognition or meters.

The browser path is lost at 754210 ms; a new system track starts there while
the microphone continues. At 900000 ms the person chooses Me, finalising the
system track and recording both the selection and the `none/user` source event.
The microphone continues to 960000 ms. The final selection is Me only, so the
fallback line is absent then. No return to the browser is inferred.

Readers ignore unknown event types and fields. A v1 folder without events has
no inferred fallback line. Export mixes the three WAVs by their explicit offsets;
events themselves are not exported in wave 6.
