FROM ubuntu:26.04
# Bound mirror acquisition and reject incomplete indexes instead of using stale ones.
RUN printf '%s\n' 'Acquire::Retries "5";' 'Acquire::Retries::Delay "true";' 'Acquire::Retries::Delay::Maximum "5";' 'Acquire::http::Timeout "15";' 'Acquire::https::Timeout "15";' 'APT::Update::Error-Mode "any";' > /etc/apt/apt.conf.d/80cassini-network
RUN apt-get update && apt-get install -y --no-install-recommends pulseaudio pulseaudio-utils && rm -rf /var/lib/apt/lists/*
ENV HOME=/tmp
# E1 backstop: run.sh removes this server on exit; if run.sh itself is killed, it ends
# within 2 h, longer than any bounded run.
ENTRYPOINT ["timeout", "--kill-after=30", "7200", "pulseaudio", "--daemonize=no", "--exit-idle-time=-1", "--disable-shm=yes", "--log-level=debug", "--log-time=true", "--log-target=stderr", "-n", "--file=/audio/server.pa"]
