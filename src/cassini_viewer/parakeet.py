# SPDX-License-Identifier: GPL-3.0-or-later
"""D16 token timing and the pinned sherpa C API. Import only in the speech child."""

import ctypes as C
import math
import os
import unicodedata
from pathlib import Path

from .parakeet_abi import SherpaOnnxOfflineRecognizerConfig, SherpaOnnxOfflineRecognizerResult
from .parakeet_model import verify_model
from .sherpa_runtime import PINS, verify_runtime


def round_ms(seconds):
    value = seconds * 1000
    return math.floor(value + 0.5) if value >= 0 else math.ceil(value - 0.5)


def words_from_tokens(tokens, timestamps, durations):
    if len(tokens) != len(timestamps) or len(tokens) != len(durations):
        raise ValueError("Parakeet token arrays differ in length")
    words, pieces, start, end = [], [], None, None

    def flush():
        value = "".join(pieces)
        if value:
            words.append(dict(startMs=start, endMs=end if end is not None else start, text=value))

    for token, timestamp, duration in zip(tokens, timestamps, durations, strict=True):
        if not isinstance(token, str) or not all(math.isfinite(v) and v >= 0 for v in (timestamp, duration)):
            raise ValueError("Invalid Parakeet token timing")
        starts_word = token.startswith(("\u2581", " "))
        if starts_word or start is None:
            flush()
            pieces, start, end = [], round_ms(timestamp), None
        piece = token[1:] if starts_word else token
        pieces.append(piece)
        if any(unicodedata.category(char).startswith(("L", "N")) for char in piece):
            finish = round_ms(timestamp + duration)
            end = max(end, finish) if end is not None else finish
    flush()
    return words


class Parakeet:
    initial_realtime_factor = 0.3

    def __init__(self, model, runtime):
        if os.environ.get("ORT_DISABLE_TELEMETRY") != "1":
            raise RuntimeError("ORT_DISABLE_TELEMETRY must be 1 before loading Parakeet")
        verify_model(model)
        runtime = verify_runtime(runtime)
        self.ort = C.CDLL(str(runtime / "libonnxruntime.so"), mode=C.RTLD_LOCAL)
        self.api = api = C.CDLL(str(runtime / "libsherpa-onnx-c-api.so"), mode=C.RTLD_LOCAL)
        signatures = {
            "GetVersionStr": ([], C.c_char_p),
            "CreateOfflineRecognizer": ([C.POINTER(SherpaOnnxOfflineRecognizerConfig)], C.c_void_p),
            "DestroyOfflineRecognizer": ([C.c_void_p], None),
            "CreateOfflineStream": ([C.c_void_p], C.c_void_p),
            "DestroyOfflineStream": ([C.c_void_p], None),
            "AcceptWaveformOffline": ([C.c_void_p, C.c_int32, C.POINTER(C.c_float), C.c_int32], None),
            "DecodeOfflineStream": ([C.c_void_p, C.c_void_p], None),
            "GetOfflineStreamResult": ([C.c_void_p], C.POINTER(SherpaOnnxOfflineRecognizerResult)),
            "DestroyOfflineRecognizerResult": ([C.POINTER(SherpaOnnxOfflineRecognizerResult)], None),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(api, "SherpaOnnx" + name)
            function.argtypes, function.restype = arguments, result
        self.version = api.SherpaOnnxGetVersionStr().decode()
        if self.version != PINS["nativeVersion"]:
            raise ValueError("Parakeet native version differs from D16")
        # ctypes zero-initializes the complete pinned structure, including all
        # unused models, hotwords, LM, homophone and blank-penalty fields.
        self.config = config = SherpaOnnxOfflineRecognizerConfig()
        config.feat_config.sample_rate = 16000
        config.feat_config.feature_dim = 128
        directory = Path(model).resolve(strict=True)
        self.paths = [
            os.fsencode(directory / name)
            for name in ("encoder.int8.onnx", "decoder.int8.onnx", "joiner.int8.onnx", "tokens.txt")
        ]
        config.model_config.transducer.encoder = self.paths[0]
        config.model_config.transducer.decoder = self.paths[1]
        config.model_config.transducer.joiner = self.paths[2]
        config.model_config.tokens = self.paths[3]
        config.model_config.model_type = b"nemo_transducer"
        config.model_config.provider = b"cpu"
        config.model_config.num_threads = 2
        config.decoding_method = b"greedy_search"
        self.recognizer = api.SherpaOnnxCreateOfflineRecognizer(C.byref(config))
        if not self.recognizer:
            raise RuntimeError("Could not create the pinned Parakeet recognizer")

    def transcribe(self, samples, *, language=None, word_timestamps=True):
        import numpy as np

        if language is not None or not word_timestamps:
            raise ValueError("D16 uses automatic language and token timings")
        if not self.recognizer:
            raise RuntimeError("Parakeet recognizer is closed")
        values = np.ascontiguousarray(samples, dtype=np.float32)
        if values.ndim != 1 or not 0 < len(values) < 2**31 or not np.isfinite(values).all():
            raise ValueError("Expected finite mono float32 audio")
        api = self.api
        stream = api.SherpaOnnxCreateOfflineStream(self.recognizer)
        if not stream:
            raise RuntimeError("Could not create a Parakeet segment stream")
        result = None
        try:
            api.SherpaOnnxAcceptWaveformOffline(stream, 16000, values.ctypes.data_as(C.POINTER(C.c_float)), len(values))
            api.SherpaOnnxDecodeOfflineStream(self.recognizer, stream)
            result = api.SherpaOnnxGetOfflineStreamResult(stream)
            if not result:
                raise RuntimeError("Parakeet returned no segment result")
            value = result.contents
            if (
                not 0 <= value.count <= 1_000_000
                or value.count
                and not (value.tokens_arr and value.timestamps and value.durations)
            ):
                raise ValueError("Parakeet returned invalid token arrays")
            words = words_from_tokens(
                [(value.tokens_arr[i] or b"").decode() for i in range(value.count)],
                [float(value.timestamps[i]) for i in range(value.count)],
                [float(value.durations[i]) for i in range(value.count)],
            )
            return dict(
                text=(value.text or b"").decode().strip(),
                language=(value.lang or b"").decode() or "und",
                words=[dict(start=w["startMs"] / 1000, end=w["endMs"] / 1000, word=w["text"]) for w in words],
            )
        finally:
            if result:
                api.SherpaOnnxDestroyOfflineRecognizerResult(result)
            api.SherpaOnnxDestroyOfflineStream(stream)

    def close(self):
        if self.recognizer:
            self.api.SherpaOnnxDestroyOfflineRecognizer(self.recognizer)
            self.recognizer = None
