# SPDX-License-Identifier: GPL-3.0-or-later
"""D24 Nemotron C API, instantiated only inside the existing speech worker."""

import ctypes as C
import os
import time
from pathlib import Path

from .diarization import normalize_turns
from .diarization_abi import SherpaOnnxOfflineSpeakerDiarizationConfig as Config
from .diarization_abi import SherpaOnnxOfflineSpeakerDiarizationSegment as Segment
from .nemotron_model import verify_model
from .sherpa_runtime import PINS, verify_runtime
from .whistle_worker import PRIVACY


class Nemotron:
    def __init__(self, model, runtime):
        if any(os.environ.get(key) != value for key, value in PRIVACY.items()):
            raise RuntimeError("Speech privacy flags must be set before loading Nemotron")
        verify_model(model)
        runtime = verify_runtime(runtime)
        self.ort = C.CDLL(str(runtime / "libonnxruntime.so"), mode=C.RTLD_LOCAL)
        self.api = api = C.CDLL(str(runtime / "libsherpa-onnx-c-api.so"), mode=C.RTLD_LOCAL)
        self.callback_type = C.CFUNCTYPE(C.c_int32, C.c_int32, C.c_int32, C.c_void_p)
        signatures = {
            "GetVersionStr": ([], C.c_char_p),
            "CreateOfflineSpeakerDiarization": ([C.POINTER(Config)], C.c_void_p),
            "DestroyOfflineSpeakerDiarization": ([C.c_void_p], None),
            "OfflineSpeakerDiarizationGetSampleRate": ([C.c_void_p], C.c_int32),
            "OfflineSpeakerDiarizationProcessWithCallback": (
                [C.c_void_p, C.POINTER(C.c_float), C.c_int32, self.callback_type, C.c_void_p],
                C.c_void_p,
            ),
            "OfflineSpeakerDiarizationResultGetNumSpeakers": ([C.c_void_p], C.c_int32),
            "OfflineSpeakerDiarizationResultGetNumSegments": ([C.c_void_p], C.c_int32),
            "OfflineSpeakerDiarizationResultSortByStartTime": ([C.c_void_p], C.POINTER(Segment)),
            "OfflineSpeakerDiarizationDestroySegment": ([C.POINTER(Segment)], None),
            "OfflineSpeakerDiarizationDestroyResult": ([C.c_void_p], None),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(api, "SherpaOnnx" + name)
            function.argtypes, function.restype = arguments, result
        self.version = api.SherpaOnnxGetVersionStr().decode()
        if self.version != PINS["nativeVersion"] or ".nemotron-diarization-v2" not in self.version:
            raise ValueError("The pinned runtime does not support D24 diarization")
        # Zero all unused embedding/clustering/segmentation fields. The fork
        # detects Nemotron from the model tail; there is no Sortformer C field.
        self.config = config = Config()
        self.model_bytes = os.fsencode(Path(model).resolve(strict=True) / "model.int8.onnx")
        config.segmentation.pyannote.model = self.model_bytes
        config.segmentation.num_threads = 2
        config.segmentation.provider = b"cpu"
        config.min_duration_on, config.min_duration_off = 0.3, 0.5
        self.handle = api.SherpaOnnxCreateOfflineSpeakerDiarization(C.byref(config))
        if not self.handle:
            raise RuntimeError("Could not construct the pinned Nemotron model")
        if api.SherpaOnnxOfflineSpeakerDiarizationGetSampleRate(self.handle) != 16000:
            self.close()
            raise ValueError("Nemotron sample rate differs from D24")

    def infer(self, samples, notify=lambda *_: None):
        if (
            not self.handle
            or samples.ndim != 1
            or samples.dtype.str != "<f4"
            or not samples.flags.c_contiguous
            or len(samples) > 0x7FFFFFFF
        ):
            raise ValueError("Expected contiguous mono float32 16kHz PCM and an open model")
        if len(samples) < 3200:
            return dict(turns=[], speakerCount=0, inferenceSeconds=0)
        api, result, segments = self.api, None, None

        @self.callback_type
        def callback(done, total, _context):
            # Cancellation kills this child; the fork ignores return values.
            notify(done, total)
            return 0

        started = time.monotonic()
        try:
            result = api.SherpaOnnxOfflineSpeakerDiarizationProcessWithCallback(
                self.handle, samples.ctypes.data_as(C.POINTER(C.c_float)), len(samples), callback, None
            )
            if not result:
                raise RuntimeError("Nemotron returned no result")
            count = api.SherpaOnnxOfflineSpeakerDiarizationResultGetNumSegments(result)
            speakers = api.SherpaOnnxOfflineSpeakerDiarizationResultGetNumSpeakers(result)
            if not 0 <= count <= 100000 or not 0 <= speakers <= 8:
                raise ValueError("Invalid Nemotron result dimensions")
            segments = api.SherpaOnnxOfflineSpeakerDiarizationResultSortByStartTime(result)
            if count and not segments:
                raise ValueError("Nemotron returned no turn array")
            raw = [
                dict(start=float(segments[i].start), end=float(segments[i].end), speaker=int(segments[i].speaker))
                for i in range(count)
            ]
            return dict(
                turns=normalize_turns(raw, len(samples) * 1000 // 16000),
                speakerCount=speakers,
                inferenceSeconds=time.monotonic() - started,
            )
        finally:
            if segments:
                api.SherpaOnnxOfflineSpeakerDiarizationDestroySegment(segments)
            if result:
                api.SherpaOnnxOfflineSpeakerDiarizationDestroyResult(result)

    def close(self):
        if self.handle:
            self.api.SherpaOnnxDestroyOfflineSpeakerDiarization(self.handle)
            self.handle = None
