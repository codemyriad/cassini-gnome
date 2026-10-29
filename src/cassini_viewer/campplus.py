# SPDX-License-Identifier: GPL-3.0-or-later
"""D25 pinned speaker extractor. Native loading is confined to the speech child."""

import ctypes as C
import hashlib
import math
import os
import time

from .campplus_model import verify_model
from .diarization_abi import SherpaOnnxSpeakerEmbeddingExtractorConfig as Config
from .sherpa_runtime import PINS, verify_runtime
from .voices_logic import DIMENSION, MAX_SECONDS, MIN_SECONDS, normalised
from .whistle_worker import PRIVACY

MODEL_FILE = "3dspeaker_speech_campplus_sv_en_voxceleb_16k.onnx"


class SpeakerExtractor:
    def __init__(self, model, runtime):
        if any(os.environ.get(k) != v for k, v in PRIVACY.items()):
            raise RuntimeError("Speech privacy flags must precede native construction")
        verify_model(model)
        runtime = verify_runtime(runtime)
        self.ort = C.CDLL(str(runtime / "libonnxruntime.so"), mode=C.RTLD_LOCAL)
        self.api = api = C.CDLL(str(runtime / "libsherpa-onnx-c-api.so"), mode=C.RTLD_LOCAL)
        signatures = {
            "GetVersionStr": ([], C.c_char_p),
            "CreateSpeakerEmbeddingExtractor": ([C.POINTER(Config)], C.c_void_p),
            "DestroySpeakerEmbeddingExtractor": ([C.c_void_p], None),
            "SpeakerEmbeddingExtractorDim": ([C.c_void_p], C.c_int32),
            "SpeakerEmbeddingExtractorCreateStream": ([C.c_void_p], C.c_void_p),
            "OnlineStreamAcceptWaveform": ([C.c_void_p, C.c_int32, C.POINTER(C.c_float), C.c_int32], None),
            "OnlineStreamInputFinished": ([C.c_void_p], None),
            "SpeakerEmbeddingExtractorIsReady": ([C.c_void_p, C.c_void_p], C.c_int32),
            "SpeakerEmbeddingExtractorComputeEmbedding": ([C.c_void_p, C.c_void_p], C.POINTER(C.c_float)),
            "SpeakerEmbeddingExtractorDestroyEmbedding": ([C.POINTER(C.c_float)], None),
            "DestroyOnlineStream": ([C.c_void_p], None),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(api, "SherpaOnnx" + name)
            function.argtypes, function.restype = arguments, result
        self.version = api.SherpaOnnxGetVersionStr().decode()
        if self.version != PINS["nativeVersion"]:
            raise ValueError("Unexpected native runtime")
        self.config = config = Config()
        self.path_bytes = os.fsencode(os.path.join(model, MODEL_FILE))
        config.model, config.num_threads, config.provider = self.path_bytes, 2, b"cpu"
        self.handle = api.SherpaOnnxCreateSpeakerEmbeddingExtractor(C.byref(config))
        if not self.handle:
            raise RuntimeError("Speaker embedding extractor returned NULL")
        self.dimension = api.SherpaOnnxSpeakerEmbeddingExtractorDim(self.handle)
        if self.dimension != DIMENSION:
            self.close()
            raise ValueError("Unexpected speaker embedding dimension")

    def embed(self, chunks):
        import numpy as np

        api, stream, vector = self.api, None, None
        frames, fingerprint = 0, hashlib.sha256()
        started = time.monotonic()
        try:
            stream = api.SherpaOnnxSpeakerEmbeddingExtractorCreateStream(self.handle)
            if not stream:
                raise RuntimeError("Embedding stream returned NULL")
            for chunk in chunks:
                chunk = np.ascontiguousarray(chunk, dtype="<f4")
                if chunk.ndim != 1 or not 0 < len(chunk) <= 160000 or not np.isfinite(chunk).all():
                    raise ValueError("Invalid speaker window")
                if frames + len(chunk) > MAX_SECONDS * 16000:
                    raise ValueError("Speaker print exceeds its speech budget")
                api.SherpaOnnxOnlineStreamAcceptWaveform(
                    stream, 16000, chunk.ctypes.data_as(C.POINTER(C.c_float)), len(chunk)
                )
                frames += len(chunk)
                fingerprint.update(chunk.tobytes())
            if frames < MIN_SECONDS * 16000:
                return None
            api.SherpaOnnxOnlineStreamInputFinished(stream)
            if not api.SherpaOnnxSpeakerEmbeddingExtractorIsReady(self.handle, stream):
                return None
            vector = api.SherpaOnnxSpeakerEmbeddingExtractorComputeEmbedding(self.handle, stream)
            if not vector:
                raise RuntimeError("Embedding returned NULL")
            embedding = normalised([vector[i] for i in range(self.dimension)])
            if embedding is None or not all(math.isfinite(v) for v in embedding):
                return None
            seconds = frames / 16000
            return dict(
                embedding=embedding,
                seconds=seconds,
                sampleCount=frames,
                inputSHA256=fingerprint.hexdigest(),
                realtimeFactor=(time.monotonic() - started) / seconds,
            )
        finally:
            if vector:
                api.SherpaOnnxSpeakerEmbeddingExtractorDestroyEmbedding(vector)
            if stream:
                api.SherpaOnnxDestroyOnlineStream(stream)

    def close(self):
        if self.handle:
            self.api.SherpaOnnxDestroySpeakerEmbeddingExtractor(self.handle)
            self.handle = None
