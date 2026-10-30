#!/bin/sh
# Build the Flatpak and write a single-file bundle to ./dist.
#   build-aux/flatpak/build.sh [--devel] [--install] [--smoke]
#     --devel    build io.codemyriad.CassiniTalk.Devel -> dist/cassini-talk-devel.flatpak
#     --install  install the bundle for the current user afterwards
#     --smoke    install it, then smoke-test it offline (build-aux/flatpak/smoke.sh)
# Needs flatpak plus flatpak-builder (native, or the org.flatpak.Builder app).
# The GNOME SDK and Platform are installed from Flathub (--user) if missing.
# Scratch state goes to _flatpak/, .flatpak-builder/ and repo/.
set -eu
cd "$(dirname "$0")/../.."

APP_ID=io.codemyriad.CassiniTalk
BUNDLE=cassini-talk
INSTALL=
SMOKE=
for arg in "$@"; do
  case $arg in
    --devel) APP_ID=io.codemyriad.CassiniTalk.Devel; BUNDLE=cassini-talk-devel ;;
    --install) INSTALL=1 ;;
    --smoke) SMOKE=1 ;;
    -h|--help) sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done
MANIFEST=build-aux/flatpak/$APP_ID.json

if ! command -v flatpak >/dev/null 2>&1; then
  echo "error: flatpak is not installed; see https://flatpak.org/setup/" >&2
  exit 1
fi
if command -v flatpak-builder >/dev/null 2>&1; then
  set -- flatpak-builder
elif flatpak info org.flatpak.Builder >/dev/null 2>&1; then
  # Skipping the document portal lets it run on headless hosts where the
  # portal is not mounted. The app's host access does not cover every path
  # (/tmp, for one), and where it cannot see the working directory it starts
  # in $HOME instead, so grant the checkout explicitly and start there.
  set -- flatpak run --no-documents-portal --filesystem="$PWD" --cwd="$PWD" org.flatpak.Builder
else
  cat >&2 <<'EOF'
error: flatpak-builder not found. Install it from your distribution
(package flatpak-builder) or from Flathub:
  flatpak remote-add --user --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo
  flatpak install --user flathub org.flatpak.Builder
EOF
  exit 1
fi

# --install-deps-from needs a "flathub" remote in the user installation.
flatpak remote-add --user --if-not-exists flathub \
  https://dl.flathub.org/repo/flathub.flatpakrepo

# rofiles-fuse often cannot start from inside the Builder sandbox or in
# containers; it is only an optimisation, so do without it.
"$@" --user --install-deps-from=flathub --force-clean --disable-rofiles-fuse \
  --state-dir=.flatpak-builder --repo=repo _flatpak "$MANIFEST"

mkdir -p dist
flatpak build-bundle \
  --runtime-repo=https://dl.flathub.org/repo/flathub.flatpakrepo \
  repo "dist/$BUNDLE.flatpak" "$APP_ID"
ls -l "dist/$BUNDLE.flatpak"

if [ -n "$SMOKE" ]; then
  build-aux/flatpak/smoke.sh "dist/$BUNDLE.flatpak" "$APP_ID"
fi
if [ -n "$INSTALL" ]; then
  flatpak install --user -y --reinstall --bundle "dist/$BUNDLE.flatpak"
  echo "Run it with: flatpak run $APP_ID"
fi
