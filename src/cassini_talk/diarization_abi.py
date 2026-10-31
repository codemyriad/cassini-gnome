# SPDX-License-Identifier: GPL-3.0-or-later
"""D24 C layouts from the already pinned cassini.8 c-api.h; no native loading."""

import ctypes as C


class SherpaOnnxOfflineSpeakerSegmentationPyannoteModelConfig(C.Structure):
    _fields_ = [("model", C.c_char_p), ("window_shift_ratio", C.c_float)]


class SherpaOnnxOfflineSpeakerSegmentationModelConfig(C.Structure):
    _fields_ = [
        ("pyannote", SherpaOnnxOfflineSpeakerSegmentationPyannoteModelConfig),
        ("num_threads", C.c_int32),
        ("debug", C.c_int32),
        ("provider", C.c_char_p),
    ]


class SherpaOnnxSpeakerEmbeddingExtractorConfig(C.Structure):
    _fields_ = [("model", C.c_char_p), ("num_threads", C.c_int32), ("debug", C.c_int32), ("provider", C.c_char_p)]


class SherpaOnnxFastClusteringConfig(C.Structure):
    _fields_ = [("num_clusters", C.c_int32), ("threshold", C.c_float)]


class SherpaOnnxOfflineSpeakerDiarizationConfig(C.Structure):
    _fields_ = [
        ("segmentation", SherpaOnnxOfflineSpeakerSegmentationModelConfig),
        ("embedding", SherpaOnnxSpeakerEmbeddingExtractorConfig),
        ("clustering", SherpaOnnxFastClusteringConfig),
        ("min_duration_on", C.c_float),
        ("min_duration_off", C.c_float),
    ]


class SherpaOnnxOfflineSpeakerDiarizationSegment(C.Structure):
    _fields_ = [("start", C.c_float), ("end", C.c_float), ("speaker", C.c_int32)]
