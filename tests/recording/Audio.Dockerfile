FROM ubuntu:26.04
RUN apt-get update && apt-get install -y --no-install-recommends pulseaudio pulseaudio-utils && rm -rf /var/lib/apt/lists/*
ENV HOME=/tmp
ENTRYPOINT ["pulseaudio", "--daemonize=no", "--exit-idle-time=-1", "--disable-shm=yes", "--log-level=debug", "--log-time=true", "--log-target=stderr", "-n", "--file=/audio/server.pa"]
