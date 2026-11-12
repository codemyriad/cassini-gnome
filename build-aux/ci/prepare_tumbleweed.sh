#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# Called only by disposable build/clean-install containers, never install-deps.
# The rolling image and its repositories can publish different snapshots.
# Align the complete container instead of forcing individual dependencies.
set -eu
# shellcheck source=/dev/null
. "${CASSINI_OS_RELEASE:-/etc/os-release}"
[ "${ID:-}" = opensuse-tumbleweed ] || exit 0

repos_dir=${CASSINI_ZYPP_REPOS_DIR:-/etc/zypp/repos.d}

# MirrorCache can send repomd.xml and its content-addressed indexes to
# different mirrors during snapshot publication. Prefer openSUSE's origin
# (downloadcontent.opensuse.org) in these disposable containers so a stale
# mirror cannot turn refresh into a missing-index error, and fall back to
# openSUSE's Fastly CDN (cdn.opensuse.org) and MirrorCache
# (download.opensuse.org) when the single origin returns 503/errors.
# Keep custom repositories and signature checks intact.
set_opensuse_host() {
  host=$1
  for repository in "$repos_dir"/*.repo; do
    [ -f "$repository" ] || continue
    sed -i \
      -e "s|^baseurl=http[s]*://download\.opensuse\.org/|baseurl=https://${host}/|" \
      -e "s|^baseurl=http[s]*://cdn\.opensuse\.org/|baseurl=https://${host}/|" \
      -e "s|^baseurl=http[s]*://downloadcontent\.opensuse\.org/|baseurl=https://${host}/|" \
      "$repository"
  done
}

sync_snapshot() {
  zypper --non-interactive refresh &&
    zypper --non-interactive dist-upgrade --no-recommends --no-allow-vendor-change
}

status=0
for host in downloadcontent.opensuse.org cdn.opensuse.org download.opensuse.org; do
  set_opensuse_host "$host"
  if sync_snapshot; then
    exit 0
  else
    status=$?
  fi
  echo "prepare_tumbleweed: ${host} failed (exit ${status}); cleaning metadata before fallback" >&2
  zypper --non-interactive clean --all || true
done
exit "$status"
