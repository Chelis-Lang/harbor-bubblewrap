# The Docker backend builds this file for the seat. Apt adds Python 3.
FROM ubuntu:24.04@sha256:534baea6a22c03a63003dbc8dbe78fe34bc0d7e595d9a9dc9834884ff530eb55

RUN apt-get update && apt-get install -y --no-install-recommends python3 && rm -rf /var/lib/apt/lists/*

# The seat user 501 owns its work directory and the socket mount point.
RUN mkdir -p /shared /workspace && chown 501:501 /shared /workspace

WORKDIR /workspace
