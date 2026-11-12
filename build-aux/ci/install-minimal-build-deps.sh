#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Minimal source-validation build dependencies shared by Build and Meson tests.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
"$here/apt-install-bounded.sh" --no-install-recommends \
  meson ninja-build python3 python3-pytest python3-jsonschema python3-rfc3339-validator \
  gcc libc6-dev git ca-certificates zstd gettext libglib2.0-bin libglib2.0-dev-bin \
  desktop-file-utils appstream libgtk-4-bin
touch /etc/cassini-minimal-build-deps
