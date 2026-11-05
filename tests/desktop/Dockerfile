# SPDX-License-Identifier: GPL-3.0-or-later
# Two stages (tests/harness-image.sh): run.sh builds `harness` once per week and
# input change, CI caches it, and every run brings it up to the current archive
# and installs app.deb on top of it. BASE is pinned to a digest by run.sh.
ARG BASE=ubuntu:26.04
ARG HARNESS=harness
FROM ${BASE} AS harness
ENV DEBIAN_FRONTEND=noninteractive
COPY depends /tmp/depends
RUN rm -f /etc/dpkg/dpkg.cfg.d/excludes && apt-get update && apt-get install -y --no-install-recommends \
    gnome-shell gnome-shell-extension-appindicator dbus dbus-x11 at-spi2-core python3-gi gir1.2-atspi-2.0 \
    xdg-desktop-portal xdg-desktop-portal-gnome xdg-desktop-portal-gtk flatpak \
    pipewire pipewire-pulse wireplumber pulseaudio-utils xdg-user-dirs gsettings-desktop-schemas \
    dconf-cli fonts-cantarell adwaita-icon-theme ca-certificates \
    && apt-get satisfy -y --no-install-recommends "$(cat /tmp/depends)" \
    && rm -rf /var/lib/apt/lists/* /tmp/depends

# Every run (D68): the current archive first, so the cached harness, the base and
# the app's runtime Depends in it are never older than this run's indexes; then
# app.deb, whose own dependencies apt resolves against those same indexes.
FROM ${HARNESS}
ARG TEST_UID=1000
ARG TEST_GID=1000
ENV DEBIAN_FRONTEND=noninteractive
COPY app.deb /tmp/app.deb
RUN apt-get update && apt-get -y --no-install-recommends dist-upgrade \
    && apt-get install -y --no-install-recommends /tmp/app.deb \
    && rm -rf /var/lib/apt/lists/* /tmp/app.deb \
    && if ! getent group "$TEST_GID" >/dev/null; then groupadd -g "$TEST_GID" qa; fi \
    && useradd -o -u "$TEST_UID" -g "$TEST_GID" -d /home/qa qa && mkdir -p /home/qa \
    && chown "$TEST_UID:$TEST_GID" /home/qa
ENV HOME=/home/qa USER=qa
USER ${TEST_UID}:${TEST_GID}
