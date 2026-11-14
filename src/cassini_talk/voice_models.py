# SPDX-License-Identifier: GPL-3.0-or-later
"""Optional CAM++ weights and capability probe; no engine imports in GTK."""

import json
import math
import sys
from pathlib import Path

from . import campplus_model
from .diarization_models import DiarizationModels
from .parakeet_client import ParakeetClient
from .sherpa_runtime import PINS
from .voices_logic import DIMENSION


class VoiceProbe(ParakeetClient):
    worker_name = "CAM++"

    def worker_command(self):
        return [
            sys.executable,
            str(Path(__file__).with_name("parakeet_worker.py")),
            "--embedding-info",
            str(self.model),
            str(self.runtime),
        ]


class VoiceJobClient(VoiceProbe):
    def worker_command(self):
        return [sys.executable, str(Path(__file__).with_name("parakeet_worker.py")), "--voices"]

    def enrol_sample(self, path, frames, windows, options):
        """D-977: `{embedding, seconds}` for a guided test phrase, or None when too short."""
        packet = (
            json.dumps(
                dict(
                    type="enrolSample",
                    path=str(path),
                    frames=frames,
                    windows=[list(w) for w in windows],
                    options=dict(modelPath=options["modelPath"], runtimePath=options["runtimePath"]),
                )
            ).encode()
            + b"\n"
        )
        if len(packet) > 512 * 1024:
            raise ValueError("Voice request exceeds limit")
        self.process.stdin.write(packet)
        self.process.stdin.flush()
        value = self._receive()
        if value.get("type") == "enrol-too-short":
            return None
        if value.get("type") != "enrol-print":
            raise RuntimeError("Voice worker failed: " + str(value.get("message", "invalid reply")))
        embedding, seconds = value.get("embedding"), value.get("seconds")
        if (
            not isinstance(embedding, list)
            or len(embedding) != DIMENSION
            or not all(type(v) in (int, float) and math.isfinite(v) for v in embedding)
            or type(seconds) not in (int, float)
            or not math.isfinite(seconds)
            or seconds <= 0
        ):
            raise ValueError("Invalid voice sample print")
        return dict(embedding=embedding, seconds=float(seconds))


class VoiceModels(DiarizationModels):
    pin = campplus_model

    def __init__(self, bundle, changed, *, store, enabled=lambda: True, client_factory=VoiceProbe, defer_inspect=False):
        self.enabled = enabled
        super().__init__(bundle, changed, store=store, client_factory=client_factory, defer_inspect=defer_inspect)

    def _supported(self, info):
        return (
            info.get("model") == self.pin.MODEL_TAG
            and info.get("sampleRate") == 16000
            and type(info.get("dimension")) is int
            and info.get("dimension") == DIMENSION
            and info.get("version") == PINS["nativeVersion"]
        )

    def request(self, directory, *, origin):
        if self.cancelled.is_set() or not self.enabled():
            return None
        return super().request(directory, origin=origin)
