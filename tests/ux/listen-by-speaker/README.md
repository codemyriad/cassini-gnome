# D70 listening turns

`turns.json` is a shared, entirely synthetic English transcript and its explicit
expected listening turns. It contains no words, names, audio or timing copied
from any real meeting. All names are fictional (D70.1). Licensed CC0-1.0.

The 36-minute mock meeting has 14 speakers and 378 turns: four frequent speakers
with 127, 100, 72 and 65 turns; two with four and two turns; eight with one turn
each. The first turn has 122 words, exceeds 30 seconds and includes a long pause.
One one-turn voice speaks for only 300 ms. Layout limits and pauses must not
change these boundaries.

For both apps, apply speaker merges first, then stable-sort words by start time.
A listening turn is a maximal consecutive run of the resulting speaker ID.
Use the minimum word start and maximum word end for its bounds. Preserve the
original word indices for highlighting. An overlapping aside of another speaker
is its own turn. `expectedTurns` uses zero-based global indices; UI counters are
one-based within the selected speaker's turns.

The app tests also cover merges that rejoin runs, overlapping asides, equal start
times, non-chronological input and empty input. The vector does not prescribe
playback lead-in/tail or alter the transcript's separate paragraph model.
