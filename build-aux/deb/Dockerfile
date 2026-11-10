# Builds the .deb in a clean container. See build-aux/deb/build.sh.
# The build context is debian/, so only debian/control is sent to the engine.
ARG BASE=ubuntu:26.04
FROM ${BASE}
ENV DEBIAN_FRONTEND=noninteractive
# Ubuntu's minimal image discards MO files at package-install time. Retain our
# catalogs so the installed-package smoke can validate all six languages.
RUN printf 'path-include=/usr/share/locale/*/LC_MESSAGES/cassini-talk.mo\n' > /etc/dpkg/dpkg.cfg.d/zz-cassini-locales
# Bound mirror acquisition and reject incomplete indexes instead of using stale ones.
RUN printf '%s\n' 'Acquire::Retries "5";' 'Acquire::Retries::Delay "true";' 'Acquire::Retries::Delay::Maximum "5";' 'Acquire::http::Timeout "15";' 'Acquire::https::Timeout "15";' 'APT::Update::Error-Mode "any";' > /etc/apt/apt.conf.d/80cassini-network
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential devscripts equivs lintian && rm -rf /var/lib/apt/lists/*
COPY control /tmp/control
RUN apt-get update && mk-build-deps -i -r -t "apt-get -y --no-install-recommends" /tmp/control \
    && rm -rf /var/lib/apt/lists/*
