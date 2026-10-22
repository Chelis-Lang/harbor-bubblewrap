"""Healthcheck of the world service: exit 0 when the socket answers ping."""

import socket
import sys

try:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(2)
        client.connect("/shared/world.sock")
        client.sendall(b"ping\n")
        healthy = client.makefile().readline().strip() == "pong"
except OSError:
    healthy = False
sys.exit(0 if healthy else 1)
