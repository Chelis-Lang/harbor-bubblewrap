"""World service of the world-challenge example task.

It serves one random challenge on the Unix socket /shared/world.sock. It
records each answer in /evidence/answers.jsonl, which the seat cannot read.
The separate verifier reads that record.
"""

import json
import os
import secrets
import socket
from pathlib import Path

SOCKET = Path("/shared/world.sock")
EVIDENCE = Path("/evidence/answers.jsonl")


def record(entry: dict) -> None:
    with EVIDENCE.open("a") as file:
        file.write(json.dumps(entry) + "\n")
        file.flush()
        os.fsync(file.fileno())


def reply(line: str, challenge: int) -> str:
    if line == "ping":
        return "pong"
    if line == "challenge":
        return str(challenge)
    if line.startswith("answer "):
        value = line.removeprefix("answer ").strip()
        correct = value == str(challenge * challenge)
        record({"answer": value, "correct": correct})
        return "correct" if correct else "wrong"
    return "unknown request"


def main() -> None:
    challenge = secrets.randbelow(1_000_000) + 1
    SOCKET.unlink(missing_ok=True)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(SOCKET))
    server.listen()
    while True:
        connection, _ = server.accept()
        with connection, connection.makefile("rw") as stream:
            line = stream.readline().strip()
            stream.write(reply(line, challenge) + "\n")
            stream.flush()


if __name__ == "__main__":
    main()
