"""Exercise the trusted copy helper against a plain host directory.

The helper only needs a directory and the standard library, so these checks
run on Linux without bubblewrap. They prove the resolution rules that the
copy process relies on: an absolute link resolves inside the seat root, a
final file link is followed, and a special or directory source is refused
before any data read.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest
from harbor_bubblewrap.sandbox import TRANSFER_HELPER

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="the copy helper runs in a Linux sandbox"
)


def helper(seat: Path, output: Path, source: str, mode: str = "download-file"):
    request = {
        "mode": mode,
        "seat": str(seat),
        "source": source,
        "output": str(output),
    }
    return subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            str(TRANSFER_HELPER),
            "--request",
            json.dumps(request),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_absolute_file_link_resolves_inside_the_seat_root(tmp_path):
    seat = tmp_path / "seat"
    seat.mkdir()
    (seat / "input").write_text("seat payload")
    (seat / "etc").mkdir()
    (seat / "etc" / "input").write_text("seat etc payload")
    (seat / "link").symlink_to("/input")
    (seat / "host-link").symlink_to("/etc/input")
    output = tmp_path / "out"
    output.mkdir()
    completed = helper(seat, output, "/link")
    assert completed.returncode == 0, completed.stderr
    assert (output / "file").read_text() == "seat payload"
    (output / "file").unlink()
    completed = helper(seat, output, "/host-link")
    assert completed.returncode == 0, completed.stderr
    assert (output / "file").read_text() == "seat etc payload"


def test_directory_and_special_sources_are_refused(tmp_path):
    seat = tmp_path / "seat"
    seat.mkdir()
    (seat / "directory").mkdir()
    os.mkfifo(seat / "fifo")
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    address = str(seat / "socket")
    listener.bind(address)
    try:
        output = tmp_path / "out"
        output.mkdir()
        for source in ("/directory", "/fifo", "/socket"):
            completed = helper(seat, output, source)
            assert completed.returncode != 0, source
            assert not (output / "file").exists()
    finally:
        listener.close()


def test_directory_export_refuses_every_link(tmp_path):
    seat = tmp_path / "seat"
    seat.mkdir()
    (seat / "tree").mkdir()
    (seat / "tree" / "plain").write_text("plain")
    (seat / "tree" / "link").symlink_to("/plain")
    output = tmp_path / "out"
    output.mkdir()
    completed = helper(seat, output, "/tree", mode="download-dir")
    assert completed.returncode != 0
    (seat / "tree" / "link").unlink()
    completed = helper(seat, output, "/tree", mode="download-dir")
    assert completed.returncode == 0, completed.stderr
    assert (output / "plain").read_text() == "plain"
