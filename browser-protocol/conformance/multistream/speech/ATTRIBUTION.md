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

Changes for D52: the pinned original WAVs are stored unmodified as a.wav and
b.wav. The materializer repeats samples three times to form 48 kHz fixture
carriers, attenuates by one half, and adds the placements, gaps and -35 dB bleed
specified in corpus.json. These are fixture preparation operations, not the
application's recognition resampler. The audio has no connection to a Cassini
user's recordings. No model weights are included.
