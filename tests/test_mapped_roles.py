"""Exercise mapped ownership and private input in real Linux namespaces."""

import asyncio
import json
import os
import sys
from pathlib import Path, PurePosixPath

import pytest
from harbor_bubblewrap.holder import Holder
from harbor_bubblewrap.sandbox import TRANSFER_SOURCE, Bind, Sandbox, prepare_root
from harbor_bubblewrap.transfer import Transfer

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
                Transfer("download-file", "/private/seat", output=target)
            )
            assert copied.return_code == 0, copied.stderr
            assert (target / "file").read_bytes() == b"private seat bytes"
        finally:
            await holder.stop()
            if private:
                cleaner = Holder(
                    Sandbox(
                        root,
                        Path("/usr"),
                        closure,
                        mapped_ids=True,
                        capabilities=11,
                    )
                )
                try:
                    await cleaner.start(
                        ["sleep", "infinity"],
                        workdir="/",
                        environment={"PATH": "/usr/bin"},
                    )
                    cleaned = await cleaner.exec(
                        ["rm", "-rf", "/private"],
                        uid=0,
                        gid=0,
                        cwd="/",
                        environment={"PATH": "/usr/bin"},
                        timeout_sec=10,
                    )
                    assert cleaned.return_code == 0, cleaned.stderr
                finally:
                    await cleaner.stop()

    asyncio.run(exercise())


def test_mapped_transfers_preserve_declared_mounts_and_write_policy(tmp_path):
    async def exercise():
        root = tmp_path / "root"
        prepare_root(
            root, hostname="mounted", workdirs=("/",), resolv_conf=None, rootfs=None
        )
        shared = tmp_path / "shared"
        shared.mkdir()
        readonly = tmp_path / "readonly"
        readonly.mkdir()
        (readonly / "kept").write_bytes(b"unchanged")
        source = tmp_path / "input"
        source.write_bytes(b"host input")
        output = tmp_path / "output"
        output.mkdir()
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
                binds=(
                    Bind(shared, PurePosixPath("/shared"), True),
                    Bind(readonly, PurePosixPath("/readonly"), False),
                ),
                mapped_ids=True,
                capabilities=11,
            )
        )
        try:
            await holder.start(
                ["sleep", "infinity"], workdir="/", environment={"PATH": "/usr/bin"}
            )
            written = await holder.exec(
                ["bash", "-c", "printf 'seat output' > /shared/result"],
                uid=0,
                gid=0,
                cwd="/",
                environment={"PATH": "/usr/bin"},
                timeout_sec=10,
            )
            assert written.return_code == 0, written.stderr
            uploaded = await holder.transfer(
                Transfer(
                    "upload-file", TRANSFER_SOURCE, target="/shared/input", input=source
                )
            )
            assert uploaded.return_code == 0, uploaded.stderr
            assert (shared / "input").read_bytes() == b"host input"
            visible = await holder.exec(
                ["cat", "/shared/input"],
                uid=0,
                gid=0,
                cwd="/",
                environment={"PATH": "/usr/bin"},
                timeout_sec=10,
            )
            assert visible.return_code == 0, visible.stderr
            assert visible.stdout == "host input"
            refused = await holder.transfer(
                Transfer(
                    "upload-file",
                    TRANSFER_SOURCE,
                    target="/readonly/kept",
                    input=source,
                )
            )
            assert refused.return_code != 0
            assert (readonly / "kept").read_bytes() == b"unchanged"
            await holder.stop()
            downloaded = await holder.transfer(
                Transfer("download-file", "/shared/result", output=output)
            )
            assert downloaded.return_code == 0, downloaded.stderr
            assert (output / "file").read_bytes() == b"seat output"
        finally:
            await holder.stop()

    asyncio.run(exercise())
