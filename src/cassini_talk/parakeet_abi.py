# SPDX-License-Identifier: GPL-3.0-or-later
"""Exact D16 fork ABI, checked field by field against its pinned C header in CI."""

import ctypes as C


class SherpaOnnxFeatureConfig(C.Structure):
    _fields_ = [
        ("sample_rate", C.c_int32),
        ("feature_dim", C.c_int32),
    ]


class SherpaOnnxOfflineTransducerModelConfig(C.Structure):
    _fields_ = [
        ("encoder", C.c_char_p),
        ("decoder", C.c_char_p),
        ("joiner", C.c_char_p),
    ]


class SherpaOnnxOfflineParaformerModelConfig(C.Structure):
    _fields_ = [
        ("model", C.c_char_p),
    ]


class SherpaOnnxOfflineNemoEncDecCtcModelConfig(C.Structure):
    _fields_ = [
        ("model", C.c_char_p),
    ]


class SherpaOnnxOfflineWhisperModelConfig(C.Structure):
    _fields_ = [
        ("encoder", C.c_char_p),
        ("decoder", C.c_char_p),
        ("language", C.c_char_p),
        ("task", C.c_char_p),
        ("tail_paddings", C.c_int32),
        ("enable_token_timestamps", C.c_int32),
        ("enable_segment_timestamps", C.c_int32),
    ]


class SherpaOnnxOfflineTdnnModelConfig(C.Structure):
    _fields_ = [
        ("model", C.c_char_p),
    ]


class SherpaOnnxOfflineSenseVoiceModelConfig(C.Structure):
    _fields_ = [
        ("model", C.c_char_p),
        ("language", C.c_char_p),
        ("use_itn", C.c_int32),
    ]


class SherpaOnnxOfflineMoonshineModelConfig(C.Structure):
    _fields_ = [
        ("preprocessor", C.c_char_p),
        ("encoder", C.c_char_p),
        ("uncached_decoder", C.c_char_p),
        ("cached_decoder", C.c_char_p),
        ("merged_decoder", C.c_char_p),
    ]


class SherpaOnnxOfflineFireRedAsrModelConfig(C.Structure):
    _fields_ = [
        ("encoder", C.c_char_p),
        ("decoder", C.c_char_p),
    ]


class SherpaOnnxOfflineDolphinModelConfig(C.Structure):
    _fields_ = [
        ("model", C.c_char_p),
    ]


class SherpaOnnxOfflineZipformerCtcModelConfig(C.Structure):
    _fields_ = [
        ("model", C.c_char_p),
    ]


class SherpaOnnxOfflineCanaryModelConfig(C.Structure):
    _fields_ = [
        ("encoder", C.c_char_p),
        ("decoder", C.c_char_p),
        ("src_lang", C.c_char_p),
        ("tgt_lang", C.c_char_p),
        ("use_pnc", C.c_int32),
    ]


class SherpaOnnxOfflineWenetCtcModelConfig(C.Structure):
    _fields_ = [
        ("model", C.c_char_p),
    ]


class SherpaOnnxOfflineOmnilingualAsrCtcModelConfig(C.Structure):
    _fields_ = [
        ("model", C.c_char_p),
    ]


class SherpaOnnxOfflineMedAsrCtcModelConfig(C.Structure):
    _fields_ = [
        ("model", C.c_char_p),
    ]


class SherpaOnnxOfflineFunASRNanoModelConfig(C.Structure):
    _fields_ = [
        ("encoder_adaptor", C.c_char_p),
        ("llm", C.c_char_p),
        ("embedding", C.c_char_p),
        ("tokenizer", C.c_char_p),
        ("system_prompt", C.c_char_p),
        ("user_prompt", C.c_char_p),
        ("max_new_tokens", C.c_int32),
        ("temperature", C.c_float),
        ("top_p", C.c_float),
        ("seed", C.c_int32),
        ("language", C.c_char_p),
        ("itn", C.c_int32),
        ("hotwords", C.c_char_p),
    ]


class SherpaOnnxOfflineFireRedAsrCtcModelConfig(C.Structure):
    _fields_ = [
        ("model", C.c_char_p),
    ]


class SherpaOnnxOfflineQwen3ASRModelConfig(C.Structure):
    _fields_ = [
        ("conv_frontend", C.c_char_p),
        ("encoder", C.c_char_p),
        ("decoder", C.c_char_p),
        ("tokenizer", C.c_char_p),
        ("max_total_len", C.c_int32),
        ("max_new_tokens", C.c_int32),
        ("temperature", C.c_float),
        ("top_p", C.c_float),
        ("seed", C.c_int32),
        ("hotwords", C.c_char_p),
    ]


class SherpaOnnxOfflineCohereTranscribeModelConfig(C.Structure):
    _fields_ = [
        ("encoder", C.c_char_p),
        ("decoder", C.c_char_p),
        ("language", C.c_char_p),
        ("use_punct", C.c_int32),
        ("use_itn", C.c_int32),
    ]


class SherpaOnnxOfflineModelConfig(C.Structure):
    _fields_ = [
        ("transducer", SherpaOnnxOfflineTransducerModelConfig),
        ("paraformer", SherpaOnnxOfflineParaformerModelConfig),
        ("nemo_ctc", SherpaOnnxOfflineNemoEncDecCtcModelConfig),
        ("whisper", SherpaOnnxOfflineWhisperModelConfig),
        ("tdnn", SherpaOnnxOfflineTdnnModelConfig),
        ("tokens", C.c_char_p),
        ("num_threads", C.c_int32),
        ("debug", C.c_int32),
        ("provider", C.c_char_p),
        ("model_type", C.c_char_p),
        ("modeling_unit", C.c_char_p),
        ("bpe_vocab", C.c_char_p),
        ("telespeech_ctc", C.c_char_p),
        ("sense_voice", SherpaOnnxOfflineSenseVoiceModelConfig),
        ("moonshine", SherpaOnnxOfflineMoonshineModelConfig),
        ("fire_red_asr", SherpaOnnxOfflineFireRedAsrModelConfig),
        ("dolphin", SherpaOnnxOfflineDolphinModelConfig),
        ("zipformer_ctc", SherpaOnnxOfflineZipformerCtcModelConfig),
        ("canary", SherpaOnnxOfflineCanaryModelConfig),
        ("wenet_ctc", SherpaOnnxOfflineWenetCtcModelConfig),
        ("omnilingual", SherpaOnnxOfflineOmnilingualAsrCtcModelConfig),
        ("medasr", SherpaOnnxOfflineMedAsrCtcModelConfig),
        ("funasr_nano", SherpaOnnxOfflineFunASRNanoModelConfig),
        ("fire_red_asr_ctc", SherpaOnnxOfflineFireRedAsrCtcModelConfig),
        ("qwen3_asr", SherpaOnnxOfflineQwen3ASRModelConfig),
        ("cohere_transcribe", SherpaOnnxOfflineCohereTranscribeModelConfig),
    ]


class SherpaOnnxOfflineLMConfig(C.Structure):
    _fields_ = [
        ("model", C.c_char_p),
        ("scale", C.c_float),
    ]


class SherpaOnnxHomophoneReplacerConfig(C.Structure):
    _fields_ = [
        ("dict_dir", C.c_char_p),
        ("lexicon", C.c_char_p),
        ("rule_fsts", C.c_char_p),
    ]


class SherpaOnnxOfflineRecognizerConfig(C.Structure):
    _fields_ = [
        ("feat_config", SherpaOnnxFeatureConfig),
        ("model_config", SherpaOnnxOfflineModelConfig),
        ("lm_config", SherpaOnnxOfflineLMConfig),
        ("decoding_method", C.c_char_p),
        ("max_active_paths", C.c_int32),
        ("hotwords_file", C.c_char_p),
        ("hotwords_score", C.c_float),
        ("rule_fsts", C.c_char_p),
        ("rule_fars", C.c_char_p),
        ("blank_penalty", C.c_float),
        ("hr", SherpaOnnxHomophoneReplacerConfig),
    ]


class SherpaOnnxOfflineRecognizerResult(C.Structure):
    _fields_ = [
        ("text", C.c_char_p),
        ("timestamps", C.POINTER(C.c_float)),
        ("count", C.c_int32),
        ("tokens", C.c_char_p),
        ("tokens_arr", C.POINTER(C.c_char_p)),
        ("json", C.c_char_p),
        ("lang", C.c_char_p),
        ("emotion", C.c_char_p),
        ("event", C.c_char_p),
        ("durations", C.POINTER(C.c_float)),
        ("ys_log_probs", C.POINTER(C.c_float)),
        ("segment_timestamps", C.POINTER(C.c_float)),
        ("segment_durations", C.POINTER(C.c_float)),
        ("segment_texts", C.c_char_p),
        ("segment_texts_arr", C.POINTER(C.c_char_p)),
        ("segment_count", C.c_int32),
    ]
