# SPDX-License-Identifier: GPL-3.0-or-later
"""Optional D24 weights; acquisition and native capability checks run off GTK."""

import logging
import sys
from concurrent.futures import CancelledError
from pathlib import Path

from . import nemotron_model
from .parakeet_client import ParakeetClient
from .sherpa_runtime import runtime_path, verify_runtime
from .speech_models import SpeechModels

LOG = logging.getLogger(__name__)


class DiarizationProbe(ParakeetClient):
    worker_name = "Nemotron"

    def worker_command(self):
        return [
            sys.executable,
            str(Path(__file__).with_name("parakeet_worker.py")),
            "--diarization-info",
            str(self.model),
            str(self.runtime),
        ]


class DiarizationModels(SpeechModels):
    pin = nemotron_model
    invalid_notice = removed_notice = None

    def __init__(self, bundle, changed, *, store=None, client_factory=DiarizationProbe):
        self.bundle = bundle
        self._probe = None
        self._factor = 0.05
        super().__init__("whistle", lambda _value: None, changed, store=store, client_factory=client_factory)

    def snapshot(self):
        state = super().snapshot()
        state.setdefault("ready", False)
        return state

    def _supported(self, info):
        return (
            info.get("model") == self.pin.MODEL_TAG
            and info.get("sampleRate") == 16000
            and ".nemotron-diarization-v2" in info.get("version", "")
        )

    def _check_runtime(self, directory):
        probe = None
        try:
            probe = self.client_factory(self.bundle, directory, runtime_path(), timeout=30)
            with self.lock:
                if self.cancelled.is_set():
                    raise CancelledError()
                self._probe = probe
            probe.start()
            info = probe.ready
            if not self._supported(info):
                raise ValueError("Runtime does not support the approved speaker model")
            self._change(ready=True, error=False, runtime_error=False)
        except CancelledError:
            raise
        except Exception:
            LOG.exception("Optional diarization runtime is unavailable; keeping D11 speakers")
            self._change(ready=False, error=True, runtime_error=True)
        finally:
            if probe is not None:
                probe.close()
            with self.lock:
                self._probe = None

    def _inspect(self):
        super()._inspect()
        if not self.store._directory(self.pin.MODEL_ID, self.pin.REVISION).exists():
            self._change(downloaded=False, ready=False)

    def _verified(self):
        directory = super()._verified()
        self._check_runtime(directory)
        return directory

    def _invalid(self):
        self._change(ready=False)
        super()._invalid()

    def _download(self):
        super()._download()
        # Publication is already complete. A runtime error retains verified
        # weights for Remove/retry; it never deletes a successful download.
        self._check_runtime(self.store._directory(self.pin.MODEL_ID, self.pin.REVISION))

    def remove(self):
        with self.lock:
            if self.cancelled.is_set() or self._state["busy"]:
                return False
            self._change(ready=False)
            return super().remove()

    def retry(self):
        with self.lock:
            if self.cancelled.is_set() or self._state["busy"]:
                return False
            self._change(busy=True, error=False)
            self._launch(self._inspect)
            return True

    def request(self, directory, *, origin):
        """Called by the transcription owner, never GTK. The child rechecks pins."""
        with self.loading:
            if not self.snapshot().get("ready"):
                return None
            with self.store._lock(self.pin.MODEL_ID, self.pin.REVISION, self.cancelled):
                try:
                    model = super()._verified()
                except (OSError, ValueError):
                    model = None
                if model is not None:
                    try:
                        verify_runtime(runtime_path())
                    except (OSError, ValueError, RuntimeError):
                        self._change(ready=False, error=True, runtime_error=True)
                        return None
            if model is None:
                self._invalid()
                return None
            return dict(
                modelPath=str(model),
                runtimePath=str(runtime_path()),
                origin=origin,
                storedTurnsPath=str(directory / "speaker-turns.json"),
                realtimeFactor=self._factor,
            )

    def measured(self, factor):
        import math

        if type(factor) in (int, float) and math.isfinite(factor) and factor > 0:
            self._factor = factor

    def close(self):
        super().close()
        with self.lock:
            if self._probe is not None:
                self._probe.cancel()
