# SPDX-License-Identifier: GPL-3.0-or-later
"""D26.1 filtering belongs only to Silero, never the recognition buffers."""

import pytest

np = pytest.importorskip("numpy")
from cassini_viewer.speech_pipeline import SpeechDetector  # noqa: E402


class Session:
    def __init__(self):
        self.inputs = []

    def run(self, _outputs, inputs):
        self.inputs.append(inputs["input"].copy())
        return np.zeros((1, 1), dtype="<f4"), inputs["state"]


def detector():
    value = object.__new__(SpeechDetector)
    value.session = Session()
    value.reset()
    return value


def test_filter_state_is_carried_and_filtered_context_is_used_without_changing_input():
    value = detector()
    frame = np.full(512, -2060 / 32768, dtype="<f4")
    before = frame.tobytes()
    value.probability(frame)
    first = value.session.inputs[-1]
    assert first.shape == (1, 576) and not np.any(first[0, :64])
    assert first[0, 64] == np.float32(0.9726138984998437 * float(frame[0]))
    previous = value.hpf
    value.probability(frame)
    assert value.hpf != previous
    np.testing.assert_array_equal(value.session.inputs[-1][0, :64], first[0, -64:])
    assert frame.tobytes() == before
    value.reset()
    assert value.hpf == (0, 0, 0, 0)
    value.probability(frame)
    np.testing.assert_array_equal(value.session.inputs[-1], first)


def test_denormal_guard_and_stage_version():
    value = detector()
    value.probability(np.full(512, 1e-30, dtype="<f4"))
    assert not np.any(value.session.inputs[-1])
    assert value.input_stage == "silero-input-hpf-butter2-100hz-v1"
