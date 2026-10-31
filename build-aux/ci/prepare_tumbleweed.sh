#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# Called only by disposable build/clean-install containers, never install-deps.
# The rolling image and its repositories can publish different snapshots.
# Align the complete container instead of forcing individual dependencies.
set -eu
. /etc/os-release
[ "${ID:-}" = opensuse-tumbleweed ] || exit 0
zypper --non-interactive refresh
zypper --non-interactive dist-upgrade --no-recommends --no-allow-vendor-change
