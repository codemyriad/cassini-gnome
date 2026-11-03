# SPDX-License-Identifier: GPL-3.0-or-later
# Shared by the package and distro builds, on the host and inside their
# containers. Source it; do not run it:   . build-aux/ci/retry.sh
#
#   retry COMMAND [ARG...]
#
# Retries a network install or download (dnf, zypper, apt-get, pacman, apk,
# an image pull) up to 3 attempts in all, because mirrors and registries
# occasionally time out. D66: never wrap a build or a test in it. Every retry
# is announced as a GitHub ::warning:: annotation, so a flaky mirror stays
# visible on the run; after the third failure its exit status is returned.
retry() {
  retry_attempt=1
  while :; do
    "$@" && return 0
    retry_status=$?
    if [ "$retry_attempt" -ge 3 ]; then
      echo "retry: $1 failed $retry_attempt times (exit $retry_status): $*" >&2
      return "$retry_status"
    fi
    # On stderr: a caller may discard the command's stdout; GitHub reads both.
    echo "::warning::$1 failed (exit $retry_status, attempt $retry_attempt of 3); retrying: $*" >&2
    # A short pause gives a mirror in mid-sync or a congested registry time.
    sleep $((retry_attempt * 5))
    retry_attempt=$((retry_attempt + 1))
  done
}
