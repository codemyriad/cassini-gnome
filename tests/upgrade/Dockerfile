# SPDX-License-Identifier: GPL-3.0-or-later
FROM ubuntu:26.04
ENV DEBIAN_FRONTEND=noninteractive
COPY old.deb /tmp/old.deb
RUN apt-get update && apt-get install -y --no-install-recommends \
    /tmp/old.deb dbus-x11 dconf-cli xvfb xauth sudo \
    && rm -rf /var/lib/apt/lists/* /tmp/old.deb
RUN useradd -m qa && printf 'qa ALL=(root) NOPASSWD: /usr/bin/dpkg -i /packages/new.deb /packages/transition.deb\n' \
    > /etc/sudoers.d/cassini-upgrade && chmod 0440 /etc/sudoers.d/cassini-upgrade
USER qa
ENV HOME=/home/qa
