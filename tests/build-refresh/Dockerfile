# SPDX-License-Identifier: GPL-3.0-or-later
# Two stages (tests/harness-image.sh): run.sh builds `harness` once per week and
# input change, CI caches it, and every run installs app.deb on top of it.
ARG HARNESS=harness
FROM ubuntu:26.04 AS harness
ENV DEBIAN_FRONTEND=noninteractive
COPY depends /tmp/depends
RUN rm -f /etc/dpkg/dpkg.cfg.d/excludes && apt-get update && apt-get install -y --no-install-recommends \
    dbus-x11 xvfb xauth xdotool pulseaudio pulseaudio-utils sudo \
    at-spi2-core gir1.2-atspi-2.0 flatpak imagemagick gcc libc6-dev meson ninja-build gettext libglib2.0-dev-bin libglib2.0-bin \
    desktop-file-utils appstream fonts-cantarell \
    && apt-get satisfy -y --no-install-recommends "$(cat /tmp/depends)" \
    && rm -rf /var/lib/apt/lists/* /tmp/depends

# The app: apt resolves app.deb's own dependencies against fresh indexes.
FROM ${HARNESS}
ARG TEST_UID=1000
ARG TEST_GID=1000
ENV DEBIAN_FRONTEND=noninteractive
COPY app.deb /tmp/app.deb
RUN apt-get update && apt-get install -y --no-install-recommends /tmp/app.deb \
    && rm -rf /var/lib/apt/lists/* /tmp/app.deb
RUN if ! getent group "$TEST_GID" >/dev/null; then groupadd -g "$TEST_GID" qa; fi \
    && useradd -o -u "$TEST_UID" -g "$TEST_GID" -d /home/qa qa && mkdir -p /home/qa \
    && chown "$TEST_UID:$TEST_GID" /home/qa \
    && printf '#%s ALL=(root) NOPASSWD: /usr/bin/dpkg -i /out/upgrade-1.deb, /usr/bin/dpkg -i /out/upgrade-2.deb\n' "$TEST_UID" > /etc/sudoers.d/cassini-qa \
    && chmod 0440 /etc/sudoers.d/cassini-qa && visudo -cf /etc/sudoers.d/cassini-qa
ENV HOME=/home/qa USER=qa
USER ${TEST_UID}:${TEST_GID}
