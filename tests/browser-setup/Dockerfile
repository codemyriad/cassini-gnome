# SPDX-License-Identifier: GPL-3.0-or-later
ARG BASE=ubuntu:26.04
FROM ${BASE}
ENV DEBIAN_FRONTEND=noninteractive
ARG TEST_UID=1000
ARG TEST_GID=1000
# dbus-daemon needs a passwd entry for the runtime UID (hosted runners use
# 1001; the Ubuntu image only provides 1000). No login or host account data.
RUN if ! getent group "$TEST_GID" >/dev/null; then printf 'cassini-test:x:%s:\n' "$TEST_GID" >> /etc/group; fi \
    && if ! getent passwd "$TEST_UID" >/dev/null; then printf 'cassini-test:x:%s:%s::/tmp:/bin/sh\n' "$TEST_UID" "$TEST_GID" >> /etc/passwd; fi
COPY app.deb /tmp/app.deb
# Verify the complete package payload, including man pages that minimal OCI
# images otherwise omit. No dpkg verification exceptions.
RUN rm -f /etc/dpkg/dpkg.cfg.d/excludes && apt-get update && apt-get install -y --no-install-recommends /tmp/app.deb \
    libnss3 libnspr4 libatk1.0-0t64 libatk-bridge2.0-0t64 libatspi2.0-0t64 \
    libx11-xcb1 libxcomposite1 libxdamage1 libxrandr2 libdrm2 libgbm1 libasound2t64 \
    libcups2t64 dbus-x11 xvfb xauth libglib2.0-bin fonts-liberation \
    && rm -rf /var/lib/apt/lists/* /tmp/app.deb
