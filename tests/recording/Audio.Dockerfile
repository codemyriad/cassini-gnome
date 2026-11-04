FROM ubuntu:26.04
RUN apt-get update && apt-get install -y --no-install-recommends pulseaudio pulseaudio-utils && rm -rf /var/lib/apt/lists/*
ENV HOME=/tmp
# E1 backstop: run.sh removes this server on exit; if run.sh itself is killed, it ends
# within 2 h, longer than any bounded run.
ENTRYPOINT ["timeout", "--kill-after=30", "7200", "pulseaudio", "--daemonize=no", "--exit-idle-time=-1", "--disable-shm=yes", "--log-level=debug", "--log-time=true", "--log-target=stderr", "-n", "--file=/audio/server.pa"]
