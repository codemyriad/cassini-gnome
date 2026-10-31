# SPDX-License-Identifier: GPL-3.0-or-later
FROM ubuntu:26.04
ARG TEST_UID=1000
ARG TEST_GID=1000
ENV DEBIAN_FRONTEND=noninteractive
COPY app.deb /tmp/app.deb
RUN rm -f /etc/dpkg/dpkg.cfg.d/excludes && apt-get update && apt-get install -y --no-install-recommends \
    /tmp/app.deb gnome-shell gnome-shell-extension-appindicator dbus dbus-x11 at-spi2-core python3-gi gir1.2-atspi-2.0 \
    xdg-desktop-portal xdg-desktop-portal-gnome xdg-desktop-portal-gtk flatpak \
    pipewire pipewire-pulse wireplumber pulseaudio-utils xdg-user-dirs gsettings-desktop-schemas \
    dconf-cli fonts-cantarell adwaita-icon-theme ca-certificates \
    && rm -rf /var/lib/apt/lists/* /tmp/app.deb \
    && if ! getent group "$TEST_GID" >/dev/null; then groupadd -g "$TEST_GID" qa; fi \
    && useradd -o -u "$TEST_UID" -g "$TEST_GID" -d /home/qa qa && mkdir -p /home/qa \
    && chown "$TEST_UID:$TEST_GID" /home/qa
ENV HOME=/home/qa USER=qa
USER ${TEST_UID}:${TEST_GID}
