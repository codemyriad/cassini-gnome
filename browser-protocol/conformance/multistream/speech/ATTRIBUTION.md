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
- Speaker 1221: Dianne, utterance `1221-135767-0010` (added for corpus v2 as c.wav).

The pinned prepared-WAV mirror is `podscripter-project/test-fixtures`,
revision `6e1c1eced6e68bb35b1f1d89c56478229201a2f7`,
https://huggingface.co/datasets/podscripter-project/test-fixtures.
The mirror identifies all three utterances as LibriSpeech test-clean, CC-BY-4.0.
Decoded PCM was independently checked against the official archive, not merely
inferred from the mirror filenames. For c.wav, the PCM of
`en/librispeech_test_clean_1221_135767_0010.wav` (file sha256 08c932d1…) equals the
ffmpeg-decoded official `test-clean/1221/135767/1221-135767-0010.flac` from
https://www.openslr.org/resources/12/test-clean.tar.gz (PCM sha256 41ad783b…), and
SPEAKERS.TXT lists 1221 as Dianne, F, test-clean.

Changes for D52: the pinned original WAVs are stored unmodified as a.wav, b.wav
and c.wav. The materializer repeats samples three times to form 48 kHz fixture
carriers, attenuates by one half, and adds the placements, gaps, -35 dB bleeds
and synthetic noise bed specified in corpus.json. These are fixture preparation operations, not the
application's recognition resampler. The audio has no connection to a Cassini
user's recordings. No model weights are included.
