# Prebuilt Arch Linux package build-dependency image (build-aux/arch/build.sh).
ARG BASE=archlinux:latest@sha256:d4594bb2300cba11f41c2f27923eb56f63d9988909c402f9c95517d1e362c899
FROM ${BASE}
COPY build-aux/arch/PKGBUILD /tmp/cassini-ci/PKGBUILD
COPY build-aux/ci/retry.sh /tmp/cassini-ci/retry.sh
RUN set -eu; \
    . /tmp/cassini-ci/retry.sh; \
    printf '\nNoExtract = !usr/share/locale/*/LC_MESSAGES/cassini-talk.mo\n' >> /etc/pacman.conf; \
    retry pacman -Syu --noconfirm --needed base-devel; \
    useradd -m builder; \
    echo 'builder ALL=(ALL) NOPASSWD: ALL' > /etc/sudoers.d/builder; \
    chown -R builder: /tmp/cassini-ci; \
    deps=$(su builder -c 'cd /tmp/cassini-ci && makepkg --printsrcinfo' | sed -n 's/^\t\(make\|check\)\{0,1\}depends = //p'); \
    retry pacman -S --noconfirm --needed --asdeps $deps; \
    retry pacman -S --noconfirm --needed namcap >/dev/null || true; \
    pacman -Scc --noconfirm >/dev/null 2>&1 || true; \
    rm -rf /tmp/cassini-ci; \
    touch /etc/cassini-arch-deps-installed
