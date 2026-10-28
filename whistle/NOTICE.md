Whistle weights and the Needle engine are from Cactus Compute, Apache-2.0.
The Python wrapper is cactus-needle 3.2.0, Apache-2.0, as in Cassini Live.
The engine-only wheel supplies native engine 3.3.0. Exact sources, revisions
and checksums are in pins.json; the complete Apache-2.0 licence is included.

https://github.com/cactus-compute/needle
https://huggingface.co/Cactus-Compute/needle3
https://huggingface.co/Cactus-Compute/whistle

JFK audio/reference: public-domain US federal government material (1961
inaugural address), supplied through the shared Cassini coordination fixtures.

Silero VAD v6.2.1, snakers4/silero-vad revision
7e30209a3e901f9842f81b225f3e93d8199902b1, is MIT-licensed.
Its pinned model is bundled at build time; see LICENSE-Silero-MIT.txt.

ONNX Runtime 1.31.0 (Microsoft, MIT), which runs Silero VAD, is the pinned
upstream PyPI wheel for the package's Python (URLs and sha256 in pins.json),
installed unpacked in the app's private directory without its model tools
or C API library; see LICENSE-ONNXRuntime-MIT.txt, and
onnxruntime/ThirdPartyNotices.txt there for the third-party components it
includes.
