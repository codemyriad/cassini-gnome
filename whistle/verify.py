#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Verify the complete bundle before staging it for packaging; never load it.

verify.py BUNDLE                 every file against manifest.json
verify.py --onnxruntime DIR      the private ONNX Runtime: every file
                                 against its wheel's RECORD, built for the
                                 Python running this script
"""

import sys
import sysconfig
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from cassini_viewer.whistle_worker import verify_bundle, verify_onnxruntime  # noqa: E402

if len(sys.argv) == 3 and sys.argv[1] == "--onnxruntime":
    runtime = verify_onnxruntime(sys.argv[2])
    extension = "onnxruntime_pybind11_state" + sysconfig.get_config_var("EXT_SUFFIX")
    if not (runtime / "onnxruntime" / "capi" / extension).is_file():
        sys.exit(f"{runtime} has no {extension}: it is not for this Python")
    print(runtime)
elif len(sys.argv) == 2:
    print(verify_bundle(sys.argv[1]))
else:
    sys.exit(__doc__)
