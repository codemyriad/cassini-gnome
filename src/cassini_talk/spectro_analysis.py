# SPDX-License-Identifier: GPL-3.0-or-later
"""D36 PCM-to-linear-power core, entirely outside GTK and the WAV writer lock.

Native and browser observers share the same powers and frozen vectors. Frame
sizes follow the published power-of-two rate table; the timestamp grid stays
on the committed track, including late subscriptions and source gaps.
"""

import array
import ctypes
import ctypes.util
import math
import struct
import threading
from concurrent.futures import Future
from fractions import Fraction
from functools import lru_cache

SPEC = "cassini-spectro-v1"
BANDS = 32
EDGES = tuple(
    700
    * (
        10
        ** (
            (
                2595 * math.log10(1 + 80 / 700)
                + (2595 * math.log10(1 + 8000 / 700) - 2595 * math.log10(1 + 80 / 700)) * i / BANDS
            )
            / 2595
        )
        - 1
    )
    for i in range(BANDS + 1)
)


_PREPARATION = None
_PREPARATION_LOCK = threading.Lock()


def prepare_acceleration():
    """Warm the already-packaged numeric helper off GTK and capture callbacks.

    The small pure-array path remains available immediately and if NumPy cannot
    load. The FFT itself is always GStreamer's real libgstfft implementation.
    """
    global _PREPARATION
    with _PREPARATION_LOCK:
        if _PREPARATION is None:
            future = _PREPARATION = Future()

            def load():
                try:
                    import numpy

                    future.set_result(numpy)
                except Exception:
                    future.set_result(None)

            threading.Thread(target=load, name="cassini-spectro-prepare", daemon=True).start()
    return _PREPARATION


def acceleration():
    return _PREPARATION.result() if _PREPARATION is not None and _PREPARATION.done() else None


class Complex(ctypes.Structure):
    _fields_ = [("real", ctypes.c_float), ("imaginary", ctypes.c_float)]


@lru_cache(maxsize=1)
def library():
    name = ctypes.util.find_library("gstfft-1.0")
    if name is None:
        raise OSError("GStreamer FFT is unavailable")
    lib = ctypes.CDLL(name)
    lib.gst_fft_f32_new.argtypes = [ctypes.c_int, ctypes.c_int]
    lib.gst_fft_f32_new.restype = ctypes.c_void_p
    lib.gst_fft_f32_free.argtypes = [ctypes.c_void_p]
    lib.gst_fft_f32_window.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_float), ctypes.c_int]
    lib.gst_fft_f32_fft.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_float), ctypes.POINTER(Complex)]
    return lib


def weights(rate, size):
    result = []
    for low, high in zip(EDGES[:-1], EDGES[1:], strict=True):
        low, high = low * size / rate, high * size / rate
        bins = []
        for k in range(math.floor(low + 0.5), min(math.ceil(high + 0.5), size // 2) + 1):
            overlap = min(k + 0.5, high) - max(k - 0.5, low)
            if overlap > 1e-9:
                bins.append((k, overlap))
        result.append(bins)
    return result


def frame_size(rate):
    if type(rate) is not int or not 8000 <= rate <= 192000:
        raise ValueError("Invalid spectrogram rate")
    return 1 << ((rate * 2048 + 47999) // 48000 - 1).bit_length()


class FFT:
    def __init__(self, rate):
        self.size = frame_size(rate)
        self.lib = library()
        self.handle = self.lib.gst_fft_f32_new(self.size, False)
        if not self.handle:
            raise MemoryError("Cannot allocate GStreamer FFT")
        self.scratch = array.array("f", [0]) * self.size
        self.pointer = (ctypes.c_float * self.size).from_buffer(self.scratch)
        self.output = (Complex * (self.size // 2 + 1))()
        self.values = memoryview(self.output).cast("B").cast("f")
        self._vector = self._band_vector = None
        self.scale = 2 / (self.size * (3 * self.size / 8) * 32768**2)
        self.table = tuple(
            (bins[0][0], bins[0][1], bins[-1][0], bins[-1][1]) if bins else None for bins in weights(rate, self.size)
        )

    def close(self):
        if getattr(self, "handle", None):
            self.lib.gst_fft_f32_free(self.handle)
            self.handle = None

    def __del__(self):
        self.close()

    def powers(self, frame):
        if not self.handle or len(frame) != self.size:
            raise ValueError("Incomplete spectrogram frame")
        self.scratch[:] = frame
        # Periodic Hann confines a constant to bins 0/1, outside every band.
        # D36 permits omitting the mean subtraction; the DC golden is exact.
        np = acceleration()
        if np is not None:
            if self._vector is None:
                window = array.array("f", [1]) * self.size
                pointer = (ctypes.c_float * self.size).from_buffer(window)
                self.lib.gst_fft_f32_window(self.handle, pointer, 2)
                scratch = np.frombuffer(self.scratch, dtype=np.float32)
                self._vector = (np.asarray(window), scratch)
            window, scratch = self._vector
            np.multiply(scratch, window, out=scratch)
        else:
            self.lib.gst_fft_f32_window(self.handle, self.pointer, 2)
        self.lib.gst_fft_f32_fft(self.handle, self.pointer, self.output)
        if np is not None:
            if self._band_vector is None:
                first = np.asarray([band[0] if band else 0 for band in self.table])
                last = np.asarray([band[2] if band else 0 for band in self.table])
                first_weight = np.asarray([band[1] if band else 0 for band in self.table])
                last_weight = np.asarray([band[3] if band and band[0] != band[2] else 0 for band in self.table])
                indexes = np.column_stack((np.minimum(first + 1, last), last)).ravel()
                self._band_vector = (
                    first,
                    last,
                    first_weight,
                    last_weight,
                    indexes,
                    (last > first + 1),
                    np.empty(self.size + 2),
                    np.empty(self.size // 2 + 1),
                )
            first, last, first_weight, last_weight, indexes, interior, squared, energy = self._band_vector
            np.square(np.frombuffer(self.output, dtype=np.float32), dtype=np.float64, out=squared)
            np.add(squared[::2], squared[1::2], out=energy)
            powers = energy[first] * first_weight + energy[last] * last_weight
            powers += np.add.reduceat(energy, indexes)[::2] * interior
            return (powers * self.scale).tolist()
        values, result = self.values, []
        for band in self.table:
            if band is None:
                result.append(0.0)
                continue
            first, first_weight, last, last_weight = band
            power = (values[2 * first] ** 2 + values[2 * first + 1] ** 2) * first_weight
            if first != last:
                power += (values[2 * last] ** 2 + values[2 * last + 1] ** 2) * last_weight
                interior = values[2 * (first + 1) : 2 * last]
                if hasattr(math, "sumprod"):
                    power += math.sumprod(interior, interior)
                else:  # Source installs still support Python 3.11.
                    power += math.fsum(value * value for value in interior)
            result.append(power * self.scale)
        return result


class Track:
    """One admitted PCM stream; unselected tracks pay for a bounded raw copy."""

    def __init__(self, rate=48000, channels=1, *, start_ms=0):
        if channels not in (1, 2) or not math.isfinite(start_ms) or start_ms < 0:
            raise ValueError("Invalid spectrogram track")
        self.size, self.rate, self.channels = frame_size(rate), rate, channels
        self._fft, self._float_ready = None, False
        self.hop = round(rate * 0.020)
        self.history, self.raw_history = array.array("f"), bytearray()
        self.total, self.next_end = 0, self.hop
        self.slot_origin = Fraction(str(start_ms)) / 20 - Fraction(self.size * 25, rate)
        self.integral_hop = self.hop * 50 == rate
        self.integer_origin = self.slot_origin.numerator // self.slot_origin.denominator

    @property
    def fft(self):
        if self._fft is None:
            self._fft = FFT(self.rate)
        return self._fft

    def skip(self, frames):
        if type(frames) is not int or frames < 0:
            raise ValueError("Invalid spectrogram gap")
        if not frames:
            return
        self.total += frames
        self.history, self.raw_history = array.array("f"), bytearray()
        self._float_ready = False
        self.next_end = (self.total // self.hop + 1) * self.hop

    def _remember(self, data):
        size = max(self.size, self.rate // 10) * self.channels * 2
        if len(data) >= size:
            self.raw_history[:] = data[-size:]
        else:
            discard = max(0, len(self.raw_history) + len(data) - size)
            if discard:
                del self.raw_history[:discard]
            self.raw_history.extend(data)

    def _mono(self, data):
        # struct builds its integer tuple in C; array-to-array conversion of
        # unlike element types otherwise pays for the Python iterator per PCM
        # sample. The explicit little-endian format also covers big-endian hosts.
        np = acceleration()
        if np is not None:
            samples = np.frombuffer(data, dtype="<i2").astype(np.float32)
            if self.channels == 2:
                samples = samples.reshape(-1, 2).mean(axis=1)
            result = array.array("f")
            result.frombytes(samples.tobytes())
            return result
        samples = struct.unpack(f"<{len(data) // 2}h", data)
        if self.channels == 1:
            return array.array("f", samples)
        return array.array("f", ((left + right) / 2 for left, right in zip(samples[::2], samples[1::2], strict=True)))

    def _append(self, samples):
        self.history.extend(samples)
        if len(self.history) > self.size:
            del self.history[: -self.size]
        self.total += len(samples)

    def feed(self, data, *, selected=True, gap_frames=0):
        if len(data) % (2 * self.channels):
            raise ValueError("Partial PCM frame")
        self.skip(gap_frames)
        if not selected:
            # No sample conversion or FFT allocation for quieter participants.
            self._remember(data)
            self.history = array.array("f")
            self._float_ready = False
            if self._fft is not None:
                self._fft.close()
                self._fft = None
            self.total += len(data) // (2 * self.channels)
            self.next_end = (self.total // self.hop + 1) * self.hop
            return []
        if not self._float_ready:
            self.history = self._mono(self.raw_history[-self.size * self.channels * 2 :])
            self._float_ready = True
        mono = self._mono(data)
        result, consumed = [], 0
        while consumed < len(mono):
            count = min(self.next_end - self.total, len(mono) - consumed)
            self._append(mono[consumed : consumed + count])
            consumed += count
            if self.total == self.next_end:
                if len(self.history) == self.size:
                    if self.integral_hop:
                        slot = self.integer_origin + self.next_end // self.hop
                    else:
                        exact = self.slot_origin + Fraction(self.next_end * 50, self.rate)
                        slot = exact.numerator // exact.denominator
                    result.append((slot, self.fft.powers(self.history)))
                self.next_end += self.hop
        self._remember(data)
        return result


class Quantizer:
    """Release once per mixed lane slot, resetting after every missing slot."""

    def __init__(self, release=3):
        self.release, self.previous, self.held = release, None, None

    def column(self, slot, powers):
        if len(powers) != BANDS or any(not math.isfinite(p) or p < 0 for p in powers):
            raise ValueError("Invalid spectrogram powers")
        np = acceleration()
        if np is not None:
            db = np.maximum(powers, 1e-12)
            np.log10(db, out=db)
            db *= 10
            if self.held is not None and self.previous == slot - 1:
                np.maximum(db, np.asarray(self.held) - self.release, out=db)
            self.previous, self.held = slot, db
            codes = (db + 80) * (255 / 60)
            np.clip(codes, 0, 255, out=codes)
            np.rint(codes, out=codes)
            return codes.astype(np.uint8).tobytes()
        db = [10 * math.log10(max(power, 1e-12)) for power in powers]
        if self.held is not None and self.previous == slot - 1:
            db = [max(current, previous - self.release) for current, previous in zip(db, self.held, strict=True)]
        self.previous, self.held = slot, db
        return bytes(round(255 * min(1, max(0, (level + 80) / 60))) for level in db)
