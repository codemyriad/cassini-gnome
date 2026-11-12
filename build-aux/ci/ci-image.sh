#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# Prebuilt build-dependency images in GHCR (GNOME-CI-CACHING.md item 2): a pinned base plus what the build
# scripts would otherwise install on every run.
#
#   build-aux/ci/ci-image.sh tag KIND BASE      the image's tag (none for a base not pinned by digest)
#   build-aux/ci/ci-image.sh use KIND BASE      pull it and print it, or print BASE if it is not there
#   build-aux/ci/ci-image.sh publish KIND BASE  build, check for secrets and push it (main and schedule
#                                               only: .github/workflows/ci-images.yml); never overwrites a tag
#
# KIND: deb     build-aux/deb/Dockerfile (debian/control's build dependencies), used by build-aux/deb/build.sh
#       rpm     rpm-build, the spec's BuildRequires and rpmlint, used by build-aux/rpm/build.sh's build container
#               (its clean-install smoke container stays on the raw base: the proof that Requires resolve)
#       source  build-aux/install-deps.sh --all, used by build-aux/check-distros.sh
#
# The tag hashes every file that declares the dependencies, this script and the base's digest, and ends with
# the ISO week, like tests/harness-image.sh: a changed dependency names a new image, which pull requests do
# not publish, so the PR builds from the raw base and a missing dependency still fails there; and no image is
# older than a week. Rolling bases (nightly, unpinned) have no tag: they always use the raw base.
#
# What can enter an image: the build context is a fresh directory holding only the Dockerfile and the
# dependency files below (no checkout, no .git, no environment), there are no build secrets or build args
# other than BASE, and publish refuses to push an image whose history, config or files contain a GitHub token
# pattern or the job's own token.
set -eu
cd "$(dirname "$0")/../.."
. build-aux/container.sh
REPO=ghcr.io/codemyriad/cassini-gnome-ci
EPOCH=${CASSINI_HARNESS_EPOCH:-$(date -u +%G-W%V)}

inputs() {
  case $1 in
    deb) echo build-aux/deb/Dockerfile debian/control ;;
    rpm) echo build-aux/rpm/cassini-talk.spec build-aux/ci/prepare_tumbleweed.sh ;;
    source) echo build-aux/install-deps.sh ;;
    *) echo "unknown kind: $1" >&2; return 2 ;;
  esac
}

tag() {  # KIND BASE
  case $2 in *@sha256:*) ;; *) return 1 ;; esac
  # shellcheck disable=SC2046 # one file per word
  hash=$({ cat $(inputs "$1") build-aux/ci/ci-image.sh; printf 'base=%s\n' "$2"; } | sha256sum | cut -c1-16)
  name="$1-$(image_slug "$2")-$hash-$EPOCH"
  [ ${#name} -le 128 ] || { echo "image tag over 128 characters: $name" >&2; return 1; }
  echo "$REPO:$name"
}

context() {  # KIND DIR: the Dockerfile and only the files it needs
  case $1 in
    deb)
      cp build-aux/deb/Dockerfile "$2/Dockerfile"
      cp debian/control "$2/control"
      ;;
    rpm)
      cp build-aux/rpm/cassini-talk.spec build-aux/ci/prepare_tumbleweed.sh build-aux/ci/retry.sh "$2/"
      cat > "$2/Dockerfile" <<'DOCKERFILE'
ARG BASE
FROM ${BASE}
COPY cassini-talk.spec prepare_tumbleweed.sh retry.sh /tmp/cassini-ci/
# The same installs as build-aux/rpm/build.sh's build container, which skips them when this marker exists.
RUN set -eu; . /tmp/cassini-ci/retry.sh; spec=/tmp/cassini-ci/cassini-talk.spec; \
    mkdir -p /etc/rpm; printf '%%_install_langs en:it:fr:es:pt_BR:pt_PT\n' > /etc/rpm/macros.zz-cassini-languages; \
    if command -v dnf >/dev/null 2>&1; then \
      retry dnf -y install rpm-build tar xz "dnf-command(builddep)"; \
      retry dnf -y --setopt=install_weak_deps=False builddep "$spec"; \
      retry dnf -y install rpmlint || true; \
      dnf clean all; \
    elif command -v zypper >/dev/null 2>&1; then \
      retry sh /tmp/cassini-ci/prepare_tumbleweed.sh; \
      retry zypper -n install rpm-build tar xz; \
      retry zypper -n install $(rpmspec -q --buildrequires "$spec" | tr -d ' '); \
      retry zypper -n install rpmlint || true; \
      zypper -n clean -a; \
    else echo "no dnf or zypper" >&2; exit 1; fi; \
    rm -rf /tmp/cassini-ci; touch /etc/cassini-rpm-deps-installed
DOCKERFILE
      ;;
    source)
      cp build-aux/install-deps.sh build-aux/ci/retry.sh "$2/"
      cat > "$2/Dockerfile" <<'DOCKERFILE'
ARG BASE
FROM ${BASE}
COPY install-deps.sh retry.sh /tmp/cassini-ci/
# The same install as build-aux/check-distros.sh, which skips it when this marker exists.
RUN set -eu; . /tmp/cassini-ci/retry.sh; \
    if command -v dnf >/dev/null 2>&1; then retry sh /tmp/cassini-ci/install-deps.sh --all --yes --required-only; dnf clean all; \
    else retry sh /tmp/cassini-ci/install-deps.sh --all --yes; fi; \
    rm -rf /tmp/cassini-ci /var/lib/apt/lists/*; touch /etc/cassini-deps-installed
DOCKERFILE
      ;;
  esac
}

case ${1:-} in
  tag)
    tag "${2:?kind}" "${3:?base}"
    ;;
  use)
    if image=$(tag "${2:?kind}" "${3:?base}") && "$CONTAINER_ENGINE" pull -q "$image" >/dev/null 2>&1; then
      echo "using prebuilt $image" >&2
      echo "$image"
    else
      echo "no prebuilt $2 image for $3: building from the base" >&2
      echo "$3"
    fi
    ;;
  publish)
    kind=${2:?kind} base=${3:?base}
    image=$(tag "$kind" "$base") || { echo "$base is not pinned by digest: nothing to publish" >&2; exit 2; }
    if "$CONTAINER_ENGINE" manifest inspect "$image" >/dev/null 2>&1; then
      echo "$image exists: not overwritten"; exit 0
    fi
    ctx=$(mktemp -d)
    trap 'rm -rf "$ctx"' EXIT
    context "$kind" "$ctx"
    echo "build context: $(ls "$ctx" | tr '\n' ' ')"
    "$CONTAINER_ENGINE" build --pull --label org.opencontainers.image.source=https://github.com/codemyriad/cassini-gnome \
      --build-arg BASE="$base" -t "$image" "$ctx"
    # No secret may leave in the image: its history and config, then every file, must hold neither a GitHub
    # token pattern nor this job's token itself.
    pattern='gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}'
    if { "$CONTAINER_ENGINE" history --no-trunc "$image"; "$CONTAINER_ENGINE" image inspect "$image"; } | grep -E -q "$pattern"; then
      echo "refusing to push $image: a token pattern in its history or config" >&2; exit 1
    fi
    hits=0
    if [ -n "${GH_TOKEN:-}" ]; then
      secret=$(mktemp)                          # the token as a grep pattern file, never on a command line
      chmod 600 "$secret" && printf '%s\n' "$GH_TOKEN" > "$secret"
      cid=$("$CONTAINER_ENGINE" create "$image" true)
      hits=$("$CONTAINER_ENGINE" export "$cid" | grep -a -c -F -f "$secret" || true)
      "$CONTAINER_ENGINE" rm "$cid" >/dev/null
      rm -f "$secret"
    fi
    [ "$hits" = 0 ] || { echo "refusing to push $image: the job's token is in its files" >&2; exit 1; }
    "$CONTAINER_ENGINE" push -q "$image"
    echo "published $image $("$CONTAINER_ENGINE" image inspect --format '{{index .RepoDigests 0}}' "$image")"
    ;;
  *)
    sed -n '4,9p' "$0" >&2
    exit 2
    ;;
esac
