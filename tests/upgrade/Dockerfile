# SPDX-License-Identifier: GPL-3.0-or-later
FROM ubuntu:26.04
ENV DEBIAN_FRONTEND=noninteractive
COPY old.deb /tmp/old.deb
RUN apt-get update && apt-get install -y --no-install-recommends \
    /tmp/old.deb dbus-x11 dconf-cli xvfb xauth xdotool sudo pulseaudio pulseaudio-utils \
    meson ninja-build gettext libglib2.0-bin libglib2.0-dev-bin desktop-file-utils appstream at-spi2-core gir1.2-atspi-2.0 \
    && rm -rf /var/lib/apt/lists/* /tmp/old.deb
RUN useradd -m qa && printf 'qa ALL=(root) NOPASSWD: /usr/bin/apt-get install -y --no-install-recommends /packages/new.deb /packages/transition.deb\n' \
    > /etc/sudoers.d/cassini-upgrade && chmod 0440 /etc/sudoers.d/cassini-upgrade
USER qa
ENV HOME=/home/qa
