# SPDX-License-Identifier: GPL-3.0-or-later
"""Bounded libzstd streaming, including concatenated frames and seek-table frames."""

import ctypes
import ctypes.util
from concurrent.futures import CancelledError
from functools import lru_cache

BLOCK = 128 * 1024


class Buffer(ctypes.Structure):
    _fields_ = [("data", ctypes.c_void_p), ("size", ctypes.c_size_t), ("pos", ctypes.c_size_t)]


@lru_cache(maxsize=1)
def library():
    lib = ctypes.CDLL(ctypes.util.find_library("zstd") or "libzstd.so.1")
    for name, arguments, result in [
        ("ZSTD_createDStream", [], ctypes.c_void_p),
        ("ZSTD_freeDStream", [ctypes.c_void_p], ctypes.c_size_t),
        ("ZSTD_initDStream", [ctypes.c_void_p], ctypes.c_size_t),
        ("ZSTD_DCtx_setParameter", [ctypes.c_void_p, ctypes.c_int, ctypes.c_int], ctypes.c_size_t),
        ("ZSTD_decompressStream", [ctypes.c_void_p, ctypes.POINTER(Buffer), ctypes.POINTER(Buffer)], ctypes.c_size_t),
        ("ZSTD_isError", [ctypes.c_size_t], ctypes.c_uint),
        ("ZSTD_getErrorName", [ctypes.c_size_t], ctypes.c_char_p),
    ]:
        function = getattr(lib, name)
        function.argtypes, function.restype = arguments, result
    return lib


def decode(source, target, expected_size, *, cancelled=None):
    """Never allocate a whole model or accept an incomplete/oversized expansion."""
    lib = library()
    context = lib.ZSTD_createDStream()
    if not context:
        raise MemoryError("Could not allocate Zstandard stream")
    output = ctypes.create_string_buffer(BLOCK)
    total, consumed = 0, False

    def check(value):
        if lib.ZSTD_isError(value):
            raise ValueError("Zstandard: " + lib.ZSTD_getErrorName(value).decode("utf-8", "replace"))
        return value

    try:
        hint = check(lib.ZSTD_initDStream(context))
        check(lib.ZSTD_DCtx_setParameter(context, 100, 27))  # ZSTD_d_windowLogMax: at most 128 MiB.
        while chunk := source.read(BLOCK):
            if cancelled is not None and cancelled.is_set():
                raise CancelledError()
            consumed = True
            incoming = ctypes.create_string_buffer(chunk)
            inp = Buffer(ctypes.cast(incoming, ctypes.c_void_p), len(chunk), 0)
            flush = False
            while inp.pos < inp.size or flush:
                if cancelled is not None and cancelled.is_set():
                    raise CancelledError()
                out = Buffer(ctypes.cast(output, ctypes.c_void_p), BLOCK, 0)
                hint = check(lib.ZSTD_decompressStream(context, ctypes.byref(out), ctypes.byref(inp)))
                total += out.pos
                if total > expected_size:
                    raise ValueError("Expanded model exceeds its declared size")
                if out.pos:
                    target.write(output.raw[: out.pos])
                flush = out.pos == BLOCK and hint != 0
        if not consumed or hint != 0 or total != expected_size:
            raise ValueError("Incomplete Zstandard model or incorrect expanded size")
    finally:
        lib.ZSTD_freeDStream(context)
    return total
