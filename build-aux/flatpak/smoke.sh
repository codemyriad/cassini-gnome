#!/bin/sh
# Install a Flatpak bundle for the current user and smoke-test it the way the
# package scripts test theirs: the startup check, every GStreamer element, and
# (on x86_64) writing the D11 goldens down with the app's own worker, offline:
# the sandbox gets --unshare=network, so it has nothing but loopback.
#   build-aux/flatpak/smoke.sh BUNDLE.flatpak APP_ID [--without-goldens]
# --without-goldens runs every check but the two engines' golden decodes, whose
# installed payloads it still verifies offline (smoke-installed.py --engines).
# CI passes it for the devel bundle only: its engines come from the same pinned
# sources, with the same code, as the stable bundle's, which runs the full goldens
# in the same job (F, D20). Everything that depends on the application ID or the
# profile (install, Library persistence, the companion bridge, Trash, the
# installed build identity) still runs on both.
set -eu
cd "$(dirname "$0")/../.."
BUNDLE=$1 APP_ID=$2 GOLDENS=yes
case ${3:-} in
  "") ;;
  --without-goldens) GOLDENS=no ;;
  *) echo "usage: $0 BUNDLE.flatpak APP_ID [--without-goldens]" >&2; exit 2 ;;
esac
flatpak remote-add --user --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo
flatpak install --user -y --noninteractive --reinstall --bundle "$BUNDLE"
python3 tests/flatpak/library_persistence.py "$APP_ID" --output "$PWD/flatpak-library-persistence.json"
python3 tests/flatpak/companion_bridge.py "$APP_ID" --output "$PWD/flatpak-companion-bridge.json"
portal_prefix="$PWD/_ci-trash-portal"
if [ ! -x "$portal_prefix/libexec/xdg-desktop-portal" ]; then
  python3 tests/flatpak/prepare_trash_portal.py --output "$portal_prefix"
fi
python3 tests/flatpak/trash.py "$APP_ID" --output "$PWD/flatpak-trash.json" --portal-prefix "$portal_prefix"
goldens=no
if [ "$(uname -m)" = x86_64 ]; then goldens=$GOLDENS; fi
if [ "$goldens" = yes ]; then
  model_cache=${CASSINI_PARAKEET_TEST_CACHE:-${XDG_CACHE_HOME:-$HOME/.cache}/cassini/package-test-models}
  python3 tests/parakeet/prepare_model.py --cache "$model_cache"
  model=$(realpath "$model_cache/parakeet-ultra-0.6b-int8/18cb623004dd6c15999b93cb70b14d8a1f6e8f3a2a97ff733f104bfc3494f6a9")
fi
set -- -I "$PWD/build-aux/smoke-installed.py" /app/share/cassini-talk
if [ "$goldens" = yes ]; then
  set -- "$@" --whistle "$PWD/tests/whistle" --parakeet "$model" --parakeet-tests "$PWD/tests/parakeet"
elif [ "$(uname -m)" = x86_64 ]; then
  set -- "$@" --engines
fi
# --filesystem below grants only test fixture reads, not package payload.
set -- --command=python3 "$APP_ID" "$@"
if [ "$goldens" = yes ]; then
  set -- --filesystem="$model:ro" "$@"
fi
flatpak run --unshare=network --no-documents-portal \
  --filesystem="$PWD/build-aux:ro" --filesystem="$PWD/tests:ro" \
  "$@"
