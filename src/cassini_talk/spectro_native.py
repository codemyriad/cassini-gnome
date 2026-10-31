# SPDX-License-Identifier: GPL-3.0-or-later
"""D36 bounded native loops. Loaded only on the analysis/capture thread."""

import ctypes
import json
import os
from functools import lru_cache
from pathlib import Path


def helper_path():
    if build := os.environ.get("CASSINI_TEST_BUILD"):
        return Path(build) / "src/libcassini-spectro.so"
    try:
        return Path(json.loads(Path(__file__).with_name("paths.json").read_text())["spectro"])
    except (OSError, ValueError, KeyError):
        return None


@lru_cache(maxsize=1)
def helper():
    path = helper_path()
    if path is None:
        return None  # Unbuilt source checkout: keep the scalar reference path.
    return load(path)


def load(path):
    lib = ctypes.CDLL(str(path))
    lib.cassini_spectro_abi.restype = ctypes.c_int
    if lib.cassini_spectro_abi() != 1:
        raise OSError("Unsupported recording indicator helper ABI")
    fp, dp, up = (ctypes.POINTER(t) for t in (ctypes.c_float, ctypes.c_double, ctypes.c_uint))
    lib.cassini_spectro_mono.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_uint, fp]
    lib.cassini_spectro_window.argtypes = [fp, fp, ctypes.c_uint]
    lib.cassini_spectro_powers.argtypes = [ctypes.c_void_p, ctypes.c_uint, up, up, dp, dp, ctypes.c_double, dp]
    lib.cassini_spectro_codes.argtypes = [dp, dp, ctypes.c_int, ctypes.c_double, ctypes.c_void_p]
    return lib
