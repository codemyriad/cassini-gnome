#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# Install build and runtime dependencies inside the disposable alpine:edge
# Source canary container.
#
# alpine:edge is an unstable rolling canary whose upstream CDN mirrors
# periodically serve mid-sync indexes or corrupt packages (for example,
# "v2 package integrity error"). Try the primary mirror (with retry.sh's
# 3 attempts); if apk fails with an upstream integrity/fetch error, switch to
# an independent official mirror and retry; if that also fails with an upstream
# integrity/fetch error, record a distinct install-deps infrastructure skip
# marker and exit 75 so check-distros.sh can report SKIP without masking
# non-upstream errors, build failures or test failures.
set -eu

. "$(dirname "$0")/retry.sh"

INSTALL_DEPS=${CASSINI_INSTALL_DEPS:-build-aux/install-deps.sh}
REPOS_FILE=${ALPINE_REPOS_FILE:-/etc/apk/repositories}
APK_CACHE=${ALPINE_APK_CACHE:-/var/cache/apk}
LOGS_DIR=${ALPINE_INFRA_SKIP_DIR:-/logs}
FALLBACK_MIRROR=${CASSINI_ALPINE_FALLBACK_MIRROR:-https://mirrors.edge.kernel.org/alpine}

TMP_LOG_DIR=$(mktemp -d "${TMPDIR:-/tmp}/cassini-alpine-apk.XXXXXX")
trap 'rm -rf "$TMP_LOG_DIR"' EXIT

run_install() {
  log_file=$1
  install_status=0
  retry sh "$INSTALL_DEPS" --all --yes >"$log_file" 2>&1 || install_status=$?
  cat "$log_file"
  return "$install_status"
}

is_upstream_apk_error() {
  grep -Eiq 'integrity error|BAD signature|UNTRUSTED signature|corrupt|checksum|invalid magic|archive error|stream error|temporary error|network error|fetch|HTTP[S]? |TLS|SSL|timed? ?out|connection |unreachable|IO error|short read|unexpected EOF' "$1"
}

primary_status=0
run_install "$TMP_LOG_DIR/primary.log" || primary_status=$?
if [ "$primary_status" = 0 ]; then
  exit 0
fi

if ! is_upstream_apk_error "$TMP_LOG_DIR/primary.log"; then
  exit "$primary_status"
fi

echo "::warning::alpine:edge package install hit upstream apk integrity/fetch error on primary mirror; switching to fallback mirror $FALLBACK_MIRROR" >&2

if [ -f "$REPOS_FILE" ]; then
  sed -E "s|^https?://[^/]+/alpine/|${FALLBACK_MIRROR%/}/|" "$REPOS_FILE" > "$TMP_LOG_DIR/repositories"
  cp "$TMP_LOG_DIR/repositories" "$REPOS_FILE"
fi
rm -rf "${APK_CACHE:?}"/* 2>/dev/null || true

fallback_status=0
run_install "$TMP_LOG_DIR/fallback.log" || fallback_status=$?
if [ "$fallback_status" = 0 ]; then
  exit 0
fi

if ! is_upstream_apk_error "$TMP_LOG_DIR/fallback.log"; then
  exit "$fallback_status"
fi

detail=$(grep -Ei 'ERROR:|WARNING:|integrity error|temporary error|network error|fetch' "$TMP_LOG_DIR/fallback.log" | tail -n 1 | tr -d '\r\000-\037' | sed 's/\\/\\\\/g; s/"/\\"/g')
[ -n "$detail" ] || detail="apk add failed on primary and fallback mirrors"

echo "::warning::alpine:edge canary infrastructure skip: upstream apk package install failed on primary and fallback mirrors ($detail)" >&2

if [ -d "$LOGS_DIR" ]; then
  printf '{\n  "image": "alpine:edge",\n  "step": "install-deps",\n  "reason": "apk-upstream-mirror",\n  "detail": "%s"\n}\n' "$detail" > "$LOGS_DIR/infra-skipped.json"
fi

exit 75
