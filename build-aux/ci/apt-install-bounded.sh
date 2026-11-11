#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Network preparation has its own budget; never retry package configuration or tests.
set -euo pipefail

has_pkg=0
for arg in "$@"; do
  case "$arg" in
    -*) ;;
    *) has_pkg=1 ;;
  esac
done
[ "$has_pkg" -eq 1 ] || { echo "Usage: apt-install-bounded.sh [APT_FLAG...] PACKAGE..." >&2; exit 2; }

export DEBIAN_FRONTEND=noninteractive

apt_root="${CASSINI_APT_SOURCES_DIR:-/etc/apt}"
mkdir -p "$apt_root/apt.conf.d"
conf_file="$apt_root/apt.conf.d/80cassini-network"
cat > "$conf_file" <<'EOF'
Acquire::Retries "5";
Acquire::Retries::Delay "true";
Acquire::Retries::Delay::Maximum "5";
Acquire::http::Timeout "15";
Acquire::https::Timeout "15";
APT::Update::Error-Mode "any";
EOF
chmod 0644 "$conf_file"

rewrite_ubuntu_mirrors() {
  local stage="$1"
  local certs_file="${CASSINI_SSL_CERTS_FILE:-/etc/ssl/certs/ca-certificates.crt}"
  local kernel_scheme="http"
  local label="primary Ubuntu archive"
  if [ "$stage" = "kernel" ]; then
    if [ -f "$certs_file" ]; then
      kernel_scheme="https"
      label="mirrors.edge.kernel.org HTTPS mirror"
    else
      kernel_scheme="http"
      label="mirrors.edge.kernel.org HTTP mirror (PGP-verified)"
    fi
  fi

  local -a candidates=()
  shopt -s nullglob
  candidates=(
    "$apt_root/sources.list"
    "$apt_root/sources.list.d/"*.list
    "$apt_root/sources.list.d/"*.sources
    "$apt_root/"apt-mirrors*.txt
  )
  shopt -u nullglob

  local src ref existing seen_path
  for src in "${candidates[@]}"; do
    [ -f "$src" ] || continue
    while IFS= read -r ref; do
      [ -n "$ref" ] || continue
      seen_path=0
      for existing in "${candidates[@]}"; do
        if [ "$existing" = "$ref" ]; then
          seen_path=1
          break
        fi
      done
      if [ "$seen_path" -eq 0 ]; then
        candidates+=("$ref")
      fi
    done < <(sed -nE 's/.*mirror\+file:([^[:space:]]+).*/\1/p' "$src")
  done

  local tmp_raw tmp_out path base line
  tmp_raw="$(mktemp)"
  tmp_out="$(mktemp)"

  for path in "${candidates[@]}"; do
    [ -f "$path" ] || continue
    if [ "$stage" = "primary" ]; then
      sed 's#://azure\.archive\.ubuntu\.com/ubuntu#://archive.ubuntu.com/ubuntu#g' "$path" > "$tmp_raw"
    else
      sed -E \
        -e "s#(https?|mirror)://((azure\\.)?archive|security)\\.ubuntu\\.com/ubuntu/?#${kernel_scheme}://mirrors.edge.kernel.org/ubuntu/#g" \
        -e "s#mirror://mirrors\\.ubuntu\\.com/mirrors\\.txt#${kernel_scheme}://mirrors.edge.kernel.org/ubuntu/#g" \
        "$path" > "$tmp_raw"
    fi

    base="$(basename "$path")"
    case "$base" in
      apt-mirrors*)
        : > "$tmp_out"
        while IFS= read -r line || [ -n "$line" ]; do
          if ! grep -Fqx -- "$line" "$tmp_out"; then
            printf '%s\n' "$line" >> "$tmp_out"
          fi
        done < "$tmp_raw"
        ;;
      *)
        cp "$tmp_raw" "$tmp_out"
        ;;
    esac

    if ! cmp -s "$path" "$tmp_out"; then
      cat "$tmp_out" > "$path"
      printf 'Network retry: %s in %s\n' "$label" "$path"
    fi
  done

  rm -f "$tmp_raw" "$tmp_out"
}

options=(-o Acquire::Retries=1 -o Acquire::http::Timeout=15 -o Acquire::https::Timeout=15 -o APT::Update::Error-Mode=any)
for attempt in 1 2 3; do
  if timeout --kill-after=10 60 apt-get "${options[@]}" update &&
     timeout --kill-after=10 180 apt-get "${options[@]}" --download-only install -y "$@"; then
    # No network and no retry here: package/configuration failures stay failures.
    exec apt-get --no-download install -y "$@"
  fi
  if [ "$attempt" = 3 ]; then
    echo "Dependency network preparation failed after 3 bounded attempts; tests were not run." >&2
    exit 1
  fi
  if [ "$attempt" = 1 ]; then
    echo "Dependency download failed or timed out; network retry 2/3, retaining cached files and replacing the Azure Ubuntu mirror with the primary archive." >&2
    rewrite_ubuntu_mirrors primary
  else
    echo "Dependency download failed or timed out; network retry 3/3, retaining cached files and replacing Ubuntu archive/security mirrors with mirrors.edge.kernel.org." >&2
    rewrite_ubuntu_mirrors kernel
  fi
done
