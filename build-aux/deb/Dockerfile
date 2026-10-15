# Builds the .deb in a clean container. See build-aux/deb/build.sh.
ARG BASE=ubuntu:26.04
FROM ${BASE}
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential devscripts equivs lintian && rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY debian/control /tmp/control
RUN apt-get update && mk-build-deps -i -r -t "apt-get -y --no-install-recommends" /tmp/control \
    && rm -rf /var/lib/apt/lists/*
