# Minimal Ubuntu 26.04 build-tools image shared by build.yml and meson-tests.yml.
ARG BASE=ubuntu:26.04@sha256:065aa8626841f16057b78b027b464cd59c4fac36c204c72dcbf48d66f5fa8a96
FROM ${BASE}
ENV DEBIAN_FRONTEND=noninteractive
COPY build-aux/ci/apt-install-bounded.sh build-aux/ci/install-minimal-build-deps.sh /tmp/cassini-ci/
RUN /tmp/cassini-ci/install-minimal-build-deps.sh && rm -rf /tmp/cassini-ci /var/lib/apt/lists/*
