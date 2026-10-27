# Shared Library vectors — S-3a / D6 / D7

`python3 make.py OUT` needs Python 3.11+ and its standard library only. It uses no
network, desktop, audio device, app modules or current clock. OUT must be new or
empty. It builds `OUT/<case>/root` and `activity.json`; it never reads or writes
`expected/`. Every case uses now=1793107080000, Europe/Rome (27 October 2026,
14:18). Activity maps canonical IDs to recording/saving; readers normalise case.

Per D7 the suite has 13 cases: main + one combined case per rule group R1–R9,
plus empty, first-record and received-only. The longer rare-edge checklist is
not a release gate. `cases.json` is an explicit filesystem recipe. Expected
JSON files were written from the rules, using literal values with serialization
helpers, before updating or running the GNOME scanner. No app output has been
used to generate or bless them. Mac independently reviews every expected file.

Each app scans the generated root with the case activity, then emits exactly the
expected shape. `badge` is text or null. For a live badge, elapsed milliseconds
are max(0, now-startMs) when start is known, otherwise zero. Group order and titles
follow R6. `names` includes all speaking names; the formatter later applies the
three-name limit to row meta. `speakers` contains only id/name/colourIndex; naming
provenance remains in the source metadata for later UI consumers. `scan` is empty
if a successful scan has no listed meetings, failed for an unavailable root.

Main follows sample-data.json and boards 01/05/07/17/29: nine deterministic IDs,
Budget review selected by the future shell, live Weekly sync at 12:48, Client
call in progress, Interview 14, interrupted Design crit at 00:41:12, received
Onboarding, failed Interview 15, Weekly sync on 20 October, Planning Q4. The extra
Interview 15 time/duration and older meeting durations are fixture choices where
the boards leave them open. Existing board timestamps, titles and text are kept.

WAVs use the wave module, PCM16, and actual complete frame counts. Rule cases are
one second long. Board-duration WAVs contain a one-second integer square tone
then sparse silence, so their *logical* frame counts give the real 52/47/63-minute
etc. durations while disk allocation stays small. Sparse holes read as PCM zero;
no metadata-only fake duration is used. Onboarding's 34-minute Opus reference has
102001 real silent Opus packets, correct pre-skip/final trim and a matching audio
digest. It is 421 kB, not a 60-ms asset with a fabricated duration.

Assets have SHA-256/size/provenance in assets/SHA256.json. Original conformance
references (including two transcripts and bad digest) are byte-identical CC0
files from cassini-format 867f68c56292ada9464b8516dc2797003d5af9eb. New CC0
references can be rebuilt with assets/build.py; make.py only copies the committed
bytes. No Cassini app encoder is involved. All S-3a files are CC0-1.0.

`python3 verify.py` checks structure, coverage, asset pins and two independently
built trees for identical bytes, modes, file types and link targets. It does not
validate expected row values by running an app. Main UI preference/search/voice
stores are outside S-3a; this supplies the Library and its activity source only.
