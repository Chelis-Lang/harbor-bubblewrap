"""Exercise mapped ownership and private input in real Linux namespaces."""

import asyncio
import json
import os
import sys
from pathlib import Path, PurePosixPath

import pytest
from harbor_bubblewrap.holder import Holder
from harbor_bubblewrap.sandbox import Bind, Sandbox, prepare_root

pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or os.environ.get("HELL_TEST_NAMESPACES") != "1",
    reason="the Linux namespace tier selects mapped role checks",
)

SETUP = """
import os
from pathlib import Path
Path('/pinned').write_text('root-only')
os.chmod('/pinned', 0o400)
os.mkdir('/private', 0o700)
os.chown('/private', 501, 501)
"""
SEAT = """
import json
import os
import sys
from pathlib import Path
Path('/private/seat').write_text(sys.stdin.read())
try:
    Path('/pinned').read_text()
except PermissionError:
    denied = True
else:
    denied = False
print(json.dumps({
    'uid': os.getuid(),
    'gid': os.getgid(),
    'pinned_uid': Path('/pinned').stat().st_uid,
    'private_uid': Path('/private').stat().st_uid,
    'denied': denied,
}))
"""


def test_mapped_seat_cannot_read_root_file_and_stopped_tree_can_be_copied(tmp_path):
    async def exercise():
        root = tmp_path / "root"
        prepare_root(
            root, hostname="mapped", workdirs=("/",), resolv_conf=None, rootfs=None
        )
        closure = tuple(
            path
            for path in ("/usr", "/lib", "/lib64", "/etc/alternatives")
            if Path(path).exists()
        )
        holder = Holder(
            Sandbox(
                root,
                Path("/usr"),
                closure,
                mapped_ids=True,
                capabilities=11,
            )
        )
        private = False
        try:
            await holder.start(
                ["sleep", "infinity"], workdir="/", environment={"PATH": "/usr/bin"}
            )
            initialized = await holder.exec(
                ["python3.12", "-I", "-c", SETUP],
                uid=0,
                gid=0,
                cwd="/",
                environment={"PATH": "/usr/bin"},
                timeout_sec=10,
            )
            assert initialized.return_code == 0, initialized.stderr
            private = True
            identity = holder.identity()
            result = await holder.exec(
                ["python3.12", "-I", "-c", SEAT],
                uid=501,
                gid=501,
                cwd="/",
                environment={"PATH": "/usr/bin"},
                timeout_sec=10,
                stdin_data=b"private seat bytes",
            )
            assert result.return_code == 0, result.stderr
            assert json.loads(result.stdout) == {
                "uid": 501,
                "gid": 501,
                "pinned_uid": 0,
                "private_uid": 501,
                "denied": True,
            }
            assert holder.identity() == identity
            await holder.stop()
            target = tmp_path / "download"
            target.mkdir()
            copied = await holder.transfer(
                ["cp", "/private/seat", "/.harbor-transfer/download/seat"],
                [Bind(target, PurePosixPath("/.harbor-transfer/download"), True)],
                10,
            )
            assert copied.return_code == 0, copied.stderr
            assert (target / "seat").read_bytes() == b"private seat bytes"
        finally:
            await holder.stop()
            if private:
                cleaned = await holder.transfer(["rm", "-rf", "/private"], (), 10)
                assert cleaned.return_code == 0, cleaned.stderr

    asyncio.run(exercise())
