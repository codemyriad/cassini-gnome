# SPDX-License-Identifier: GPL-3.0-or-later
"""D16 per-job recognizer. No network; Python ORT 1.31 is only for shared Silero."""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from cassini_talk.parakeet_model import MODEL_TAG  # noqa: E402
from cassini_talk.speech_threads import background_worker_priority  # noqa: E402
from cassini_talk.whistle_worker import PRIVACY, emit, onnxruntime_dir, verify_bundle, verify_onnxruntime  # noqa: E402


def block_network(event, _args):
    if event.startswith("socket."):
        raise RuntimeError("Network access is disabled in the Parakeet worker")


def main(bundle, model_path, runtime_path):
    if any(os.environ.get(key) != value for key, value in PRIVACY.items()):
        raise RuntimeError("Speech privacy flags must be set before loading Parakeet")
    background_worker_priority()
    bundle = verify_bundle(bundle)
    runtime = onnxruntime_dir()
    if runtime is not None:
        sys.path.insert(0, str(verify_onnxruntime(runtime)))
    sys.addaudithook(block_network)
    from cassini_talk.diarization_audio import close_diarization_store, discard_staged_for_track
    from cassini_talk.parakeet import Parakeet
    from cassini_talk.recognition_cache import parakeet_identity
    from cassini_talk.session_follower import StdinLineReader, run_follow_loop
    from cassini_talk.speech_pipeline import process_tracks
    from cassini_talk.worker_diarization import WarmDiarizer

    model_identity = parakeet_identity(MODEL_TAG, Path(__file__).with_name("sherpa-pins.json"))
    model = Parakeet(model_path, runtime_path)
    reader = StdinLineReader(sys.stdin)
    detector = None
    prefix_store = {}
    diarization_store = {}
    warm_diarizer = WarmDiarizer()
    try:
        emit("ready", pid=os.getpid(), model=MODEL_TAG, modelIdentity=model_identity, version=model.version)
        while line := reader.readline():
            message = json.loads(line)
            msg_type = message.get("type")
            if msg_type == "quit":
                return 0
            if msg_type == "follow":
                action, detector = run_follow_loop(
                    model,
                    bundle,
                    message,
                    emit,
                    reader=reader,
                    detector=detector,
                    prefix_store=prefix_store,
                    diarization_store=diarization_store,
                    warm_diarizer=warm_diarizer,
                )
                if action != "continue":
                    return 0
                continue
            if msg_type == "follow-stop":
                if message.get("drain", True) is False:
                    prefix_store.clear()
                    close_diarization_store(diarization_store)
                    warm_diarizer.close()
                emit(
                    "follow-stopped",
                    directory=message.get("directory"),
                    segments=0,
                    prefixes=len(prefix_store),
                    diarizationStaged=len({id(v) for v in diarization_store.values()}),
                    diarizerWarmed=warm_diarizer.model is not None,
                )
                continue
            tracks = message.get("tracks")
            if msg_type != "pipeline" or not isinstance(tracks, list) or not 1 <= len(tracks) <= 64:
                raise ValueError("Expected one to 64 capture tracks")
            if any(
                not isinstance(t, dict) or not isinstance(t.get("id"), str) or not isinstance(t.get("path"), str)
                for t in tracks
            ):
                raise ValueError("Invalid capture track")
            from cassini_talk.speech_pipeline import SpeechDetector
            from cassini_talk.worker_diarization import complete, estimate

            if detector is None:
                detector = SpeechDetector(bundle / "silero_vad.onnx")
            options = message.get("diarization")
            try:
                try:
                    summary = process_tracks(
                        model,
                        tracks,
                        bundle / "silero_vad.onnx",
                        emit,
                        extra_seconds=estimate(tracks, options),
                        recognition_cache=message.get("recognitionCache"),
                        detector=detector,
                        prefix_store=prefix_store,
                        on_prefix_mismatch=lambda tr: discard_staged_for_track(diarization_store, tr),
                    )
                finally:
                    prefix_store.clear()
                summary = complete(
                    summary,
                    tracks,
                    options,
                    emit,
                    diarization_store=diarization_store,
                    warm_diarizer=warm_diarizer,
                )
            finally:
                close_diarization_store(diarization_store)
                warm_diarizer.close()
            emit("complete", model=MODEL_TAG, **summary)
            if message.get("keepForVoices"):
                model.close()
                model = None
                followup_line = reader.readline()
                if followup_line:
                    followup = json.loads(followup_line)
                    if followup.get("type") == "voiceprints":
                        from cassini_talk.worker_voices import request

                        request(followup, emit)
                    elif followup.get("type") != "quit":
                        raise ValueError("Expected voiceprint request")
            return 0
        return 0
    finally:
        close_diarization_store(diarization_store)
        warm_diarizer.close()
        reader.close()
        if model is not None:
            model.close()


def diarization_info(model_path, runtime_path):
    if any(os.environ.get(key) != value for key, value in PRIVACY.items()):
        raise RuntimeError("Speech privacy flags must be set before loading Nemotron")
    sys.addaudithook(block_network)
    from cassini_talk.nemotron import Nemotron
    from cassini_talk.nemotron_model import MODEL_TAG

    model = Nemotron(model_path, runtime_path)
    try:
        emit("ready", pid=os.getpid(), model=MODEL_TAG, version=model.version, sampleRate=16000)
        line = sys.stdin.buffer.readline(257)
        if len(line) > 256:
            raise ValueError("Model-info request exceeds limit")
        return 0
    finally:
        model.close()


def voice_job():
    if any(os.environ.get(key) != value for key, value in PRIVACY.items()):
        raise RuntimeError("Speech privacy flags must precede voice extraction")
    sys.addaudithook(block_network)
    emit("ready", pid=os.getpid())
    line = sys.stdin.buffer.readline(512 * 1024 + 1)
    if len(line) > 512 * 1024:
        raise ValueError("Voice request exceeds limit")
    message = json.loads(line)
    if message.get("type") == "voiceprints":
        from cassini_talk.worker_voices import request

        request(message, emit)
    elif message.get("type") == "enrolSample":
        from cassini_talk.worker_voices import enrol_sample

        enrol_sample(message, emit)
    elif message.get("type") != "quit":
        raise ValueError("Expected voiceprint request")
    return 0


def embedding_info(model_path, runtime_path):
    if any(os.environ.get(key) != value for key, value in PRIVACY.items()):
        raise RuntimeError("Speech privacy flags must precede native construction")
    sys.addaudithook(block_network)
    from cassini_talk.campplus import SpeakerExtractor
    from cassini_talk.campplus_model import MODEL_TAG

    model = SpeakerExtractor(model_path, runtime_path)
    try:
        emit(
            "ready",
            pid=os.getpid(),
            model=MODEL_TAG,
            version=model.version,
            sampleRate=16000,
            dimension=model.dimension,
        )
        line = sys.stdin.buffer.readline(257)
        if len(line) > 256:
            raise ValueError("Model-info request exceeds limit")
        return 0
    finally:
        model.close()


if __name__ == "__main__":
    try:
        if sys.argv[1:] == ["--voices"]:
            sys.exit(voice_job())
        if len(sys.argv) == 4 and sys.argv[1] == "--diarization-info":
            sys.exit(diarization_info(*sys.argv[2:]))
        if len(sys.argv) == 4 and sys.argv[1] == "--embedding-info":
            sys.exit(embedding_info(*sys.argv[2:]))
        sys.exit(main(*sys.argv[1:]))
    except Exception as error:
        emit("error", message=str(error))
        sys.exit(1)
