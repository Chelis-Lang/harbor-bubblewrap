# The Docker backend builds this file for the world service. Apt adds Python 3.
FROM ubuntu:24.04@sha256:534baea6a22c03a63003dbc8dbe78fe34bc0d7e595d9a9dc9834884ff530eb55

RUN apt-get update && apt-get install -y --no-install-recommends python3 && rm -rf /var/lib/apt/lists/*

# The world user 501 owns the socket and evidence mount points.
RUN mkdir -p /shared /evidence && chown 501:501 /shared /evidence

COPY world.py health.py /opt/world/
