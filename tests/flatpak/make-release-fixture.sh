#!/bin/sh
# Regenerate tests/flatpak/release-fixture: a real signed Flatpak repository
# and bundles, made by flatpak and ostree with throwaway keys, for
# tests/test_flatpak_release_guard.py (G15). Needs a container engine:
#   tests/flatpak/make-release-fixture.sh
# The fixture holds two commits of a tiny app: `stale.flatpak` is the first,
# `current.flatpak` and the repository's ref are the second, both signed by
# the "release" key; `other.gpg` is a second key that signed nothing.
set -eu
cd "$(dirname "$0")/../.."
. build-aux/container.sh
out=tests/flatpak/release-fixture
rm -rf "$out"
mkdir -p "$out"
container_run -v "$PWD/$out":/out:rw$Z debian:testing sh -euc '
apt-get update -qq >/dev/null
apt-get install -y -qq --no-install-recommends flatpak ostree gnupg >/dev/null
export GNUPGHOME=$(mktemp -d)
gpg --batch --passphrase "" --quick-gen-key "Cassini release fixture <release@fixture.invalid>" ed25519 sign never 2>/dev/null
gpg --batch --passphrase "" --quick-gen-key "Unrelated fixture <other@fixture.invalid>" ed25519 sign never 2>/dev/null
release=$(gpg --list-keys --with-colons release@fixture.invalid | sed -n "s/^fpr:*\([0-9A-F]*\):.*/\1/p" | head -n1)
gpg --export "$release" > /out/release.gpg
gpg --export other@fixture.invalid > /out/other.gpg
app=$(mktemp -d)
mkdir -p "$app/files/bin" "$app/export"
printf "[Application]\nname=io.codemyriad.CassiniTalk\nruntime=org.gnome.Platform/x86_64/50\nsdk=org.gnome.Sdk/x86_64/50\ncommand=hello\n" > "$app/metadata"
bundle() {
  flatpak build-bundle --repo-url=https://cassini.talk/download/linux/flatpak/ --gpg-keys=/out/release.gpg \
    --runtime-repo=https://dl.flathub.org/repo/flathub.flatpakrepo /out/repo "/out/$1" io.codemyriad.CassiniTalk master
}
for version in 1 2; do
  printf "#!/bin/sh\necho %s\n" "$version" > "$app/files/bin/hello"
  flatpak build-export --gpg-sign="$release" --gpg-homedir="$GNUPGHOME" --timestamp=2026-11-0${version}T00:00:00Z \
    /out/repo "$app" master
  flatpak build-update-repo --gpg-sign="$release" --gpg-homedir="$GNUPGHOME" /out/repo
  [ "$version" = 2 ] || bundle stale.flatpak
done
bundle current.flatpak
flatpak --version > /out/VERSIONS
ostree --version | head -n2 >> /out/VERSIONS
chown -R "$(stat -c %u:%g /out)" /out
'
find "$out" -type f | sort
