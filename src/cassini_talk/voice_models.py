# SPDX-License-Identifier: GPL-3.0-or-later
"""Optional CAM++ weights and capability probe; no engine imports in GTK."""

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


class VoiceModels(DiarizationModels):
    pin = campplus_model

    def __init__(self, bundle, changed, *, enabled=lambda: True, store=None, client_factory=VoiceProbe):
        self.enabled = enabled
        super().__init__(bundle, changed, store=store, client_factory=client_factory)

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
