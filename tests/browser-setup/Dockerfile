# SPDX-License-Identifier: GPL-3.0-or-later
# Two stages (tests/harness-image.sh): run.sh builds `harness` once per week and
# input change, CI caches it, and every run brings it up to the current archive
# and installs app.deb on top of it. BASE is pinned to a digest by run.sh.
ARG BASE=ubuntu:26.04
ARG HARNESS=harness
FROM ${BASE} AS harness
ENV DEBIAN_FRONTEND=noninteractive
COPY depends /tmp/depends
# Verify the complete package payload, including man pages that minimal OCI
# images otherwise omit. No dpkg verification exceptions.
RUN rm -f /etc/dpkg/dpkg.cfg.d/excludes && apt-get update && apt-get install -y --no-install-recommends \
    libnss3 libnspr4 libatk1.0-0t64 libatk-bridge2.0-0t64 libatspi2.0-0t64 \
    libx11-xcb1 libxcomposite1 libxdamage1 libxrandr2 libdrm2 libgbm1 libasound2t64 \
    libcups2t64 dbus-x11 xvfb xauth libglib2.0-bin fonts-liberation \
    && apt-get satisfy -y --no-install-recommends "$(cat /tmp/depends)" \
    && rm -rf /var/lib/apt/lists/* /tmp/depends

# Every run (D68): the current archive first, so the cached harness, the base and
# the app's runtime Depends in it are never older than this run's indexes; then
# app.deb, whose own dependencies apt resolves against those same indexes.
FROM ${HARNESS}
ENV DEBIAN_FRONTEND=noninteractive
ARG TEST_UID=1000
ARG TEST_GID=1000
# dbus-daemon needs a passwd entry for the runtime UID (hosted runners use
# 1001; the Ubuntu image only provides 1000). No login or host account data.
RUN if ! getent group "$TEST_GID" >/dev/null; then printf 'cassini-test:x:%s:\n' "$TEST_GID" >> /etc/group; fi \
    && if ! getent passwd "$TEST_UID" >/dev/null; then printf 'cassini-test:x:%s:%s::/tmp:/bin/sh\n' "$TEST_UID" "$TEST_GID" >> /etc/passwd; fi
COPY app.deb /tmp/app.deb
RUN apt-get update && apt-get -y --no-install-recommends dist-upgrade \
    && apt-get install -y --no-install-recommends /tmp/app.deb \
    && rm -rf /var/lib/apt/lists/* /tmp/app.deb
