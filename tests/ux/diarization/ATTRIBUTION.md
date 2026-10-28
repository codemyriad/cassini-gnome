# Audio attribution

Source: LibriSpeech ASR corpus, test-clean, prepared by Vassil Panayotov,
Guoguo Chen, Daniel Povey and Sanjeev Khudanpur from LibriVox audiobooks.

- Dataset: https://www.openslr.org/12
- Licence: Creative Commons Attribution 4.0 International (CC-BY-4.0),
  https://creativecommons.org/licenses/by/4.0/
- Citation: V. Panayotov, G. Chen, D. Povey and S. Khudanpur,
  “LibriSpeech: An ASR corpus based on public domain audio books,” ICASSP 2015.
- Speaker 1089: Peter Bobbe, utterance `1089-134686-0000`.
- Speaker 260: Brad Bush, utterance `260-123440-0004`.

The pinned prepared-WAV mirror is `podscripter-project/test-fixtures`,
revision `6e1c1eced6e68bb35b1f1d89c56478229201a2f7`,
https://huggingface.co/datasets/podscripter-project/test-fixtures.
The mirror identifies both utterances as LibriSpeech test-clean, CC-BY-4.0.
Decoded PCM was independently checked against the official archive, not merely
inferred from the mirror filenames.

Changes: convert the original FLAC samples losslessly to WAV; truncate each
source to 166960 frames, repeat each twice, multiply each sample by one half
(truncate toward zero), and mix at the placements recorded in fetch-pins.json.
One two-second overlap and two half-second gaps are introduced. The input has
no connection to a real Cassini user's recordings.

This audio licence is separate from the Nemotron model licence, OpenMDW-1.1.
Model weights are not included in this conformance snapshot.
