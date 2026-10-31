# SPDX-License-Identifier: GPL-3.0-or-later
"""One Parakeet child per queue job, using the bounded shared worker transport."""

import os
import sys
from pathlib import Path

from .whistle_client import WhistleClient
from .whistle_worker import PRIVACY


class ParakeetClient(WhistleClient):
    worker_name = "Parakeet"

    def __init__(self, bundle, model, runtime, *, timeout=120):
        super().__init__(bundle, timeout=timeout)
        self.model, self.runtime = Path(model), Path(runtime)

    def worker_command(self):
        return [
            sys.executable,
            str(Path(__file__).with_name("parakeet_worker.py")),
            str(self.bundle),
            str(self.model),
            str(self.runtime),
        ]

    def worker_environment(self):
        env = dict(os.environ)
        env.update(PRIVACY)
        env.update(OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1")
        # Whistle's CPU shim belongs only to its own child.
        env.pop("LD_PRELOAD", None)
        env.pop("NEEDLE3_LIB_PATH", None)
        return env
