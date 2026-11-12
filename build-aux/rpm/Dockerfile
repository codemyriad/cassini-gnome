# Prebuilt RPM build-dependency image (build-aux/rpm/build.sh).
ARG BASE=fedora:44@sha256:15da97ed9aa94dd3d92c3925eb89520f6c80e07ea7ee24cba848d1f8b6912b3e
FROM ${BASE}
COPY build-aux/rpm/cassini-talk.spec /tmp/cassini-ci/cassini-talk.spec
COPY build-aux/ci/retry.sh build-aux/ci/prepare_tumbleweed.sh /tmp/cassini-ci/
RUN set -eu; \
    . /tmp/cassini-ci/retry.sh; \
    spec=/tmp/cassini-ci/cassini-talk.spec; \
    if command -v dnf >/dev/null 2>&1; then \
      mkdir -p /etc/rpm; \
      printf '%%_install_langs en:it:fr:es:pt_BR:pt_PT\n' > /etc/rpm/macros.zz-cassini-languages; \
      printf 'keepcache=True\n' >> /etc/dnf/dnf.conf; \
      retry dnf -y install rpm-build tar xz "dnf-command(builddep)"; \
      retry dnf -y --setopt=install_weak_deps=False builddep "$spec"; \
      retry dnf -y install rpmlint || true; \
      dnf clean all >/dev/null 2>&1 || true; \
    elif command -v zypper >/dev/null 2>&1; then \
      retry sh /tmp/cassini-ci/prepare_tumbleweed.sh; \
      retry zypper -n install rpm-build tar xz; \
      requires=$(rpmspec -q --buildrequires "$spec" | tr -d " "); \
      retry zypper -n install $requires; \
      retry zypper -n install rpmlint || true; \
      zypper -n clean -a >/dev/null 2>&1 || true; \
    else \
      echo "error: no dnf or zypper in this image" >&2; exit 1; \
    fi; \
    rm -rf /tmp/cassini-ci; \
    touch /etc/cassini-rpm-deps-installed
