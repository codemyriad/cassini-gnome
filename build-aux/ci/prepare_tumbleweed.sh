#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# Called only by disposable build/clean-install containers, never install-deps.
# The rolling image and its repositories can publish different snapshots.
# Align the complete container instead of forcing individual dependencies.
set -eu
. /etc/os-release
[ "${ID:-}" = opensuse-tumbleweed ] || exit 0
# MirrorCache can send repomd.xml and its content-addressed indexes to
# different mirrors during snapshot publication. Use openSUSE's origin in
# these disposable containers so a stale mirror cannot turn refresh into a
# missing-index error. Keep custom repositories and signature checks intact.
for repository in /etc/zypp/repos.d/*.repo; do
  [ -f "$repository" ] || continue
  sed -i 's|^baseurl=http[s]*://download\.opensuse\.org/|baseurl=https://downloadcontent.opensuse.org/|' "$repository"
done
zypper --non-interactive refresh
zypper --non-interactive dist-upgrade --no-recommends --no-allow-vendor-change
