#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# Install build and runtime dependencies inside the Stage 1 alpine:edge
# Source canary install container.
#
# alpine:edge is an unstable rolling canary whose upstream CDN mirrors
# periodically serve mid-sync indexes or corrupt packages (for example,
# "v2 package integrity error"). Try the primary mirror (with retry.sh's
# 3 attempts); if the final attempt fails solely with explicit upstream apk
# integrity/fetch ERROR diagnostics, switch to an independent official mirror
# and retry; if that final attempt also fails solely with explicit upstream apk
# ERROR diagnostics, record a distinct install-deps infrastructure skip marker
# in ALPINE_INSTALL_OUTCOME_DIR (outside test-writable /logs) and exit 75.
set -eu

. "$(dirname "$0")/retry.sh"

INSTALL_DEPS=${CASSINI_INSTALL_DEPS:-build-aux/install-deps.sh}
REPOS_FILE=${ALPINE_REPOS_FILE:-/etc/apk/repositories}
APK_CACHE=${ALPINE_APK_CACHE:-/var/cache/apk}
OUTCOME_DIR=${ALPINE_INSTALL_OUTCOME_DIR:-}
OS_RELEASE_FILE=${ALPINE_OS_RELEASE:-/etc/os-release}
FALLBACK_MIRROR=${CASSINI_ALPINE_FALLBACK_MIRROR:-https://mirrors.edge.kernel.org/alpine}

TMP_LOG_DIR=$(mktemp -d "${TMPDIR:-/tmp}/cassini-alpine-apk.XXXXXX")
trap 'rm -rf "$TMP_LOG_DIR"' EXIT

apk_install_attempt() {
  attempt_log=$1
  shift
  attempt_status=0
  "$@" >"$attempt_log" 2>&1 || attempt_status=$?
  cat "$attempt_log"
  return "$attempt_status"
}

run_install() {
  log_file=$1
  rm -f "$log_file"
  install_status=0
  retry apk_install_attempt "$log_file" sh "$INSTALL_DEPS" --all --yes || install_status=$?
  return "$install_status"
}

is_upstream_error_line() {
  err_line=$1
  printf '%s\n' "$err_line" | grep -Eq '^ERROR: ' || return 1
  if printf '%s\n' "$err_line" | grep -Eiq '^ERROR: .+: ((v[0-9]+ )?(package |adb )?integrity error|bad signature|untrusted signature|invalid magic|(corrupted|corrupt|truncated) (archive|package|data|stream|header)|checksum (error|mismatch)|unexpected (eof|end of file)|short read|(archive|stream|deflate|gzip|zstd) error|temporary error( \(try again later\))?|network error( \(check internet connection\))?|dns lookup error|(operation |connection )?timed out|(software caused )?connection (refused|reset|reset by peer|closed|abort(ed)?)|(host|network) is unreachable|http(/[0-9.]+| error)? (404|408|429|5[0-9][0-9])([ :][^:]*)?|bad gateway|service unavailable|gateway time-?out)$'; then
    return 0
  fi
  if printf '%s\n' "$err_line" | grep -Eiq '^ERROR: (fetching )?https?://[^ ]+: (no such file or directory|file unavailable.*|not found|remote server (returned error|closed connection|unexpectedly closed connection).*|(tls|ssl)( [a-z0-9 _()-]+)?|certificate (verify failed|expired|not yet valid|error)|handshake failed|(io|i/o|input/output) error)$'; then
    return 0
  fi
  return 1
}

is_upstream_apk_error() {
  err_file="$TMP_LOG_DIR/errors.txt"
  tr -d '\r' < "$1" | grep '^ERROR: ' > "$err_file" || return 1
  [ -s "$err_file" ] || return 1
  while IFS= read -r err_line; do
    if ! is_upstream_error_line "$err_line"; then
      return 1
    fi
  done < "$err_file"
  return 0
}

write_outcome() {
  status_value=$1
  if [ -z "$OUTCOME_DIR" ] || [ ! -d "$OUTCOME_DIR" ]; then
    return 0
  fi
  rm -f "$OUTCOME_DIR/infra-skipped.json" "$OUTCOME_DIR/outcome.json" "$OUTCOME_DIR/os-release"
  if [ -f "$OS_RELEASE_FILE" ]; then
    cp "$OS_RELEASE_FILE" "$OUTCOME_DIR/os-release"
  fi
  printf '{\n  "image": "alpine:edge",\n  "step": "install-deps",\n  "status": "%s"\n}\n' "$status_value" > "$OUTCOME_DIR/outcome.json"
}

primary_status=0
run_install "$TMP_LOG_DIR/primary.log" || primary_status=$?
if [ "$primary_status" = 0 ]; then
  write_outcome installed
  exit 0
fi

if ! is_upstream_apk_error "$TMP_LOG_DIR/primary.log"; then
  write_outcome failed
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
  write_outcome installed
  exit 0
fi

if ! is_upstream_apk_error "$TMP_LOG_DIR/fallback.log"; then
  write_outcome failed
  exit "$fallback_status"
fi

detail=$(tr -d '\r' < "$TMP_LOG_DIR/fallback.log" | grep '^ERROR: ' | tail -n 1 | tr -d '\000-\037' | sed 's/\\/\\\\/g; s/"/\\"/g')

echo "::warning::alpine:edge canary infrastructure skip: upstream apk package install failed on primary and fallback mirrors ($detail)" >&2

if [ -n "$OUTCOME_DIR" ] && [ -d "$OUTCOME_DIR" ]; then
  rm -f "$OUTCOME_DIR/outcome.json" "$OUTCOME_DIR/infra-skipped.json" "$OUTCOME_DIR/os-release"
  if [ -f "$OS_RELEASE_FILE" ]; then
    cp "$OS_RELEASE_FILE" "$OUTCOME_DIR/os-release"
  fi
  printf '{\n  "image": "alpine:edge",\n  "step": "install-deps",\n  "reason": "apk-upstream-mirror",\n  "detail": "%s"\n}\n' "$detail" > "$OUTCOME_DIR/infra-skipped.json"
fi

exit 75
