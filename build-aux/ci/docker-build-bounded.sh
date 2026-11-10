#!/usr/bin/env bash
# Copyright 2026 Codemyriad OÜ
# SPDX-License-Identifier: GPL-3.0-or-later
set -euo pipefail
exec python3 "$(dirname "$0")/docker_build_watchdog.py" "$@"
