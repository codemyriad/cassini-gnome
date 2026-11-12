# Prebuilt Source-check dependency image (build-aux/check-distros.sh).
ARG BASE=ubuntu:26.04@sha256:065aa8626841f16057b78b027b464cd59c4fac36c204c72dcbf48d66f5fa8a96
FROM ${BASE}
COPY build-aux/install-deps.sh /tmp/cassini-ci/build-aux/install-deps.sh
COPY build-aux/ci/retry.sh build-aux/ci/prepare_tumbleweed.sh build-aux/ci/alpine_edge_install.sh /tmp/cassini-ci/build-aux/ci/
RUN set -eu; \
    cd /tmp/cassini-ci; \
    . build-aux/ci/retry.sh; \
    if [ -f /etc/alpine-release ]; then \
      sh build-aux/ci/alpine_edge_install.sh; \
      rm -rf /var/cache/apk/*; \
    else \
      if command -v apt-get >/dev/null 2>&1; then \
        mkdir -p /etc/apt/apt.conf.d; \
        printf '%s\n' 'Acquire::Retries "5";' 'Acquire::Retries::Delay "true";' 'Acquire::Retries::Delay::Maximum "5";' 'Acquire::http::Timeout "15";' 'Acquire::https::Timeout "15";' 'APT::Update::Error-Mode "any";' > /etc/apt/apt.conf.d/80cassini-network; \
      fi; \
      retry sh build-aux/ci/prepare_tumbleweed.sh; \
      if command -v dnf >/dev/null 2>&1; then \
        retry sh build-aux/install-deps.sh --all --yes --required-only; \
        dnf clean all >/dev/null 2>&1 || true; \
      else \
        DEBIAN_FRONTEND=noninteractive retry sh build-aux/install-deps.sh --all --yes; \
        rm -rf /var/lib/apt/lists/*; \
        ! command -v zypper >/dev/null 2>&1 || zypper -n clean -a >/dev/null 2>&1 || true; \
        ! command -v pacman >/dev/null 2>&1 || pacman -Scc --noconfirm >/dev/null 2>&1 || true; \
      fi; \
    fi; \
    rm -rf /tmp/cassini-ci; \
    touch /etc/cassini-deps-installed
