# SPDX-License-Identifier: GPL-3.0-or-later
"""Fixed pinned-model adapter shared by the offline CLI and its queue regression.

The offline probe supplies its verified model directory and runtime explicitly;
normal app acquisition/default policy remains in SpeechModels.
"""

from cassini_talk.parakeet_client import ParakeetClient
from cassini_talk.parakeet_model import MODEL_ID


class Models:
    def __init__(self, model, runtime, *, client_factory=ParakeetClient):
        self.model, self.runtime = model, runtime
        self.client_factory = client_factory
        self.clients, self.children = [], []

    def choice_for_job(self):
        return MODEL_ID

    def start_parakeet(self, bundle, *, register):
        assert all(client.process is None for client in self.clients), "Previous speech worker still alive"
        client = self.client_factory(bundle, self.model, self.runtime, timeout=120)
        register(client)
        client.start()
        self.children.append(client.process)
        self.clients.append(client)
        return client
