# SPDX-License-Identifier: GPL-3.0-or-later
"""App-owned optional speech weights. All disk/network work runs away from GTK."""

import logging
import threading
from concurrent.futures import CancelledError

from . import parakeet_model
from .model_download import IntegrityError, ModelStore, _write
from .model_manifest import NOTICE
from .parakeet_client import ParakeetClient
from .received import regular_input
from .sherpa_runtime import runtime_path

LOG = logging.getLogger(__name__)


class SpeechModels:
    pin = parakeet_model
    invalid_notice = "models.parakeetUltra.invalidFallback"
    removed_notice = "models.parakeetUltra.removedFallback"

    def __init__(self, selected, persist, changed, *, store=None, client_factory=ParakeetClient):
        self.store = store if store is not None else ModelStore()
        self.persist, self.changed = persist, changed
        self.client_factory = client_factory
        self.lock = threading.RLock()
        self.loading = threading.Lock()
        self.cancelled = threading.Event()
        self.download_cancelled = threading.Event()
        self._selected = selected if selected in ("whistle", self.pin.MODEL_ID) else "whistle"
        self._state = dict(downloaded=False, busy=True, downloading=False, percent=0, error=False, notice=None)
        self._thread = None
        self._launch(self._inspect)

    def snapshot(self):
        with self.lock:
            return dict(self._state, selected=self._selected)

    def _change(self, **values):
        with self.lock:
            if all(self._state.get(key) == value for key, value in values.items()):
                return
            self._state.update(values)
        if not self.cancelled.is_set():
            self.changed()

    def _choice(self, value):
        with self.lock:
            self._selected = value
            self.persist(value)

    def select(self, value):
        with self.lock:
            if (
                value not in ("whistle", self.pin.MODEL_ID)
                or value == self.pin.MODEL_ID
                and not self._state["downloaded"]
            ):
                return False
            self._choice(value)
        self._change(notice=None)
        return True

    def _launch(self, operation):
        # A stalled HTTPS read must never join a non-daemon executor at exit.
        self._thread = threading.Thread(target=self._operate, args=(operation,), daemon=True, name="cassini-model")
        self._thread.start()

    def _operate(self, operation):
        try:
            with self.loading:
                operation()
        except CancelledError:
            pass
        except Exception:
            LOG.exception("Optional speech model operation failed")
            self._change(error=True)
        finally:
            self._change(busy=False, downloading=False)

    def _invalid(self):
        self._choice("whistle")
        self._change(downloaded=False, notice=self.invalid_notice)
        self.store.remove(self.pin.MODEL_ID, self.pin.REVISION, cancelled=self.cancelled)

    def _verified(self):
        # Receipt, notice, complete tree and pinned model files form one
        # installed revision. Check each weight once, against the D16 pins.
        if not self.store.installed(
            self.pin.MODEL_ID,
            self.pin.REVISION,
            expected_files={name: (size, sha) for name, (size, sha, _compressed) in self.pin.FILES.items()},
            cancelled=self.cancelled,
        ):
            raise ValueError("Installed Parakeet revision is damaged")
        directory = self.store._directory(self.pin.MODEL_ID, self.pin.REVISION)
        # About uses the local root notice. Restore it from the verified
        # revision before enabling the model, including after manual damage.
        self.store._check_directories(self.store.root)
        with regular_input(directory / NOTICE) as source:
            _write(self.store.root / "NOTICE.txt", source.read())
        return directory

    def _inspect(self):
        try:
            directory = self.store._directory(self.pin.MODEL_ID, self.pin.REVISION)
        except (OSError, ValueError):
            self._invalid()
            return
        if not directory.exists():
            if self.snapshot()["selected"] == self.pin.MODEL_ID:
                self._invalid()
            return
        # Serialize inspection with a different process removing the revision.
        with self.store._lock(self.pin.MODEL_ID, self.pin.REVISION, self.cancelled):
            try:
                self._verified()
            except (OSError, ValueError):
                invalid = True
            else:
                invalid = False
                self._change(downloaded=True)
        if invalid:
            self._invalid()

    def download(self):
        with self.lock:
            if self.cancelled.is_set() or self._state["busy"]:
                return False
            self.download_cancelled = threading.Event()
            self._change(busy=True, downloading=True, error=False, notice=None, percent=0)
            self._launch(self._download)
        return True

    def _download(self):
        event = self.download_cancelled
        manifest = self.store.fetch_manifest(cancelled=event)
        model = self.pin.check_manifest(manifest.model(self.pin.MODEL_ID, self.pin.REVISION))
        paths = {file.artifact.key: file.path for file in model.files}
        completed, current = {}, {}

        def progress(phase, key, count, total):
            if phase == "downloading":
                current[paths[key]] = count
            elif phase == "verifying":
                completed[key] = total
                current.pop(key, None)
            value = min(99, int((sum(current.values()) + sum(completed.values())) * 100 / self.pin.DOWNLOAD_BYTES))
            self._change(percent=max(self.snapshot()["percent"], value))

        try:
            self.store.download(
                self.pin.MODEL_ID, self.pin.REVISION, manifest=manifest, cancelled=event, progress=progress
            )
        except IntegrityError:
            # Try again must be able to repair a damaged published revision,
            # rather than repeatedly asking for a terminal-only removal.
            if self.store._directory(self.pin.MODEL_ID, self.pin.REVISION).exists():
                self._invalid()
            raise
        # ModelStore verified the exact manifest and notice before the atomic
        # publish. There is no second cancellable pass after publication.
        # A completed explicit download never selects itself.
        self._change(downloaded=True, percent=100)

    def cancel_download(self):
        self.download_cancelled.set()

    def remove(self):
        with self.lock:
            if self.cancelled.is_set() or self._state["busy"]:
                return False
            self._choice("whistle")
            self._change(downloaded=False, busy=True, error=False, notice=self.removed_notice)
            self._launch(lambda: self.store.remove(self.pin.MODEL_ID, self.pin.REVISION, cancelled=self.cancelled))
        return True

    def start_parakeet(self, bundle, *, register):
        """Read choice at job start; Remove waits only until weights are loaded.

        register makes the child cancellable by the queue before start(). A
        loaded recognizer owns its weights independently of the cache folder.
        """
        with self.loading:
            if self.snapshot()["selected"] != self.pin.MODEL_ID:
                return None
            with self.store._lock(self.pin.MODEL_ID, self.pin.REVISION, self.cancelled):
                try:
                    directory = self._verified()
                except (OSError, ValueError):
                    invalid = True
                else:
                    invalid = False
                    client = self.client_factory(bundle, directory, runtime_path(), timeout=120)
                    register(client)
                    client.start()
            if invalid:
                self._invalid()
                return None
            return client

    def close(self):
        self.cancelled.set()
        self.download_cancelled.set()
