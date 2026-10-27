# Builds the .deb in a clean container. See build-aux/deb/build.sh.
# The build context is debian/, so only debian/control is sent to the engine.
ARG BASE=ubuntu:26.04
FROM ${BASE}
ENV DEBIAN_FRONTEND=noninteractive
# Mirrors occasionally time out, in CI especially.
# A stalled connection fails after 15 s and is retried instead of hanging.
RUN printf 'Acquire::Retries "3";\nAcquire::http::Timeout "15";\n' > /etc/apt/apt.conf.d/80retries
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential devscripts equivs lintian && rm -rf /var/lib/apt/lists/*
COPY control /tmp/control
RUN apt-get update && mk-build-deps -i -r -t "apt-get -y --no-install-recommends" /tmp/control \
    && rm -rf /var/lib/apt/lists/*
