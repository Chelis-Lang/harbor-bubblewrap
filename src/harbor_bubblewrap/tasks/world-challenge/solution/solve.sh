#!/bin/bash
# Ask the world for its challenge, then send the square of it as the answer.
set -euo pipefail

python3 - <<'EOF'
import socket


def ask(line):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect("/shared/world.sock")
        client.sendall((line + "\n").encode())
        return client.makefile().readline().strip()


challenge = int(ask("challenge"))
print(ask(f"answer {challenge * challenge}"))
EOF
