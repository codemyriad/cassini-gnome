"""Shared PCM16 measurements for conformance readers. Stdlib only, Python 3.9+."""
import math


def goertzel(samples, rate, frequency):
    """Normalized power of one channel's PCM16 samples at `frequency`.

    A full-scale sine at that frequency reads 1/4, so 10*log10(4*power) is its
    peak level in dBFS. Pass one channel (samples[channel::channels]).
    """
    n = len(samples)
    coeff = 2 * math.cos(2 * math.pi * frequency / rate)
    q1 = q2 = 0.0
    for sample in samples:
        q0 = sample + coeff * q1 - q2
        q2 = q1
        q1 = q0
    return (q1 * q1 + q2 * q2 - coeff * q1 * q2) / (n * n * 32768 ** 2)
