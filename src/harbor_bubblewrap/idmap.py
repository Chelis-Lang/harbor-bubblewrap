"""Delegate root and role identities without host-root execution."""

from __future__ import annotations

import os
import pwd
import shutil
from dataclasses import dataclass
from pathlib import Path

from .process import run


@dataclass(frozen=True)
class IdMap:
    uid_start: int
    uid_count: int
    gid_start: int
    gid_count: int

    @classmethod
    def current(cls) -> IdMap:
        if os.geteuid() == 0:
            raise ValueError("mapped role sandboxes need an unprivileged host user")
        name = pwd.getpwuid(os.getuid()).pw_name
        uid = _range(Path("/etc/subuid"), name)
        gid = _range(Path("/etc/subgid"), name)
        return cls(*uid, *gid)

    async def install(self, pid: int, timeout_sec: float) -> None:
        rows = (
            ("newuidmap", os.getuid(), self.uid_start, self.uid_count),
            ("newgidmap", os.getgid(), self.gid_start, self.gid_count),
        )
        for helper, own, start, count in rows:
            executable = shutil.which(helper)
            if executable is None:
                raise FileNotFoundError(f"{helper} is not on PATH")
            result = await run(
                [executable, str(pid), "0", str(own), "1", "1", str(start), str(count)],
                timeout_sec,
                env={},
            )
            if result.return_code:
                raise RuntimeError(
                    f"{helper} failed with exit {result.return_code}: {result.stderr}"
                )


def _range(path: Path, name: str) -> tuple[int, int]:
    owners = {name, str(os.getuid())}
    for line in path.read_text().splitlines():
        fields = line.split(":")
        if len(fields) != 3 or fields[0] not in owners:
            continue
        start, count = int(fields[1]), int(fields[2])
        if start > 0 and count >= 65536:
            return start, count
    raise ValueError(f"{path} has no delegated range of at least 65536 identities")
