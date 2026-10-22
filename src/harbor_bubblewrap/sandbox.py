"""Bubblewrap command lines for one sandbox over a persistent root directory.

The root is a host directory that holds the writable file tree of the sandbox.
One long-lived bubblewrap process, the holder, keeps the namespaces of the
sandbox alive. Commands join those namespaces (see ``enter.py``), so their
files, processes and network persist like those of a container. Short copy
processes use the same file tree in namespaces of their own.

Inside the sandbox, the store paths of the Nix environment closure are
read-only binds, and the ``bin`` directory of the environment is ``/usr/bin``.
The sandbox sees no other host path, except the binds that the caller names.
"""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

# Programs that the backend and Harbor itself run inside every sandbox.
REQUIRED_TOOLS = ("bash", "chmod", "cp", "dirname", "find", "mkdir", "rm", "sleep")

PATH = "/usr/bin:/bin"

# Mount point for the binds of one copy process. A tmpfs covers it in each copy
# process, so its mount points never reach the persistent root.
TRANSFER_DIR = PurePosixPath("/.harbor-transfer")

# The dynamic loader of a copy process reads this file from the writable root,
# which a seat can write. The empty file below covers it, so a seat cannot load
# its own library into a process that writes to a host bind.
PRELOAD = "/etc/ld.so.preload"
EMPTY_PRELOAD = Path(__file__).with_name("ld.so.preload")

PASSWD = "root:x:0:0:root:/root:/bin/bash\nnobody:x:65534:65534:nobody:/nonexistent:/bin/false\n"
GROUP = "root:x:0:\nnogroup:x:65534:\n"


@dataclass(frozen=True)
class Bind:
    """A host path that the sandbox sees at ``target``."""

    source: Path
    target: PurePosixPath
    writable: bool


@dataclass(frozen=True)
class Sandbox:
    root: Path
    environment: Path
    closure: tuple[str, ...]
    binds: tuple[Bind, ...] = ()
    share_network: bool = False
    uid: int = 0
    gid: int = 0
    hostname: str = "main"
    mapped_ids: bool = False
    capabilities: int = 0
    no_new_privs: bool = True
    transfer: bool = False

    def holder_command(
        self,
        argv: Sequence[str],
        *,
        workdir: str,
        environment: Mapping[str, str],
        info_fd: int,
        block_fd: int | None = None,
    ) -> list[str]:
        """Return the bwrap argv of the long-lived process of this sandbox."""
        args = self._namespace_args(self.uid, self.gid) + self._layout_args()
        args += ["--info-fd", str(info_fd)]
        if self.mapped_ids:
            if block_fd is None:
                raise ValueError("mapped sandbox launch needs an identity-map gate")
            args += ["--userns-block-fd", str(block_fd)]
            argv = self._bootstrap_command(argv)
        return args + _process_args(argv, workdir, environment)

    def transfer_command(self, argv: Sequence[str], binds: Sequence[Bind]) -> list[str]:
        """Return the bwrap argv of a copy process over the same file tree.

        A copy process runs as user 0 in namespaces of its own, so a link in
        the sandbox resolves inside the sandbox, not on the host. It loads no
        library named by the root: an empty preload file covers one seat path.
        """
        args = self._namespace_args(0, 0) + self._layout_args()
        args += ["--tmpfs", str(TRANSFER_DIR)]
        # A bind creates its mount point in the root. Cover a preload file only
        # when the root has one, so a copy leaves the root as it found it.
        preload = self.root / sandbox_relative(PRELOAD)
        if preload.exists() or preload.is_symlink():
            args += ["--ro-bind", str(EMPTY_PRELOAD), PRELOAD]
        for bind in binds:
            args += _bind_args(bind)
        environment = {"PATH": str(self.environment / "bin"), "HOME": "/root"}
        return args + _process_args(argv, "/", environment)

    def _bootstrap_command(self, argv: Sequence[str]) -> list[str]:
        return [
            "/usr/bin/python3.12",
            "-I",
            "-B",
            "/.harbor-init.py",
            "--bootstrap",
            str(self.uid),
            str(self.gid),
            str(self.capabilities),
            str(int(self.no_new_privs)),
            json.dumps(list(argv)),
        ]

    def _namespace_args(self, uid: int, gid: int) -> list[str]:
        executable = shutil.which("bwrap")
        if executable is None:
            raise FileNotFoundError("bwrap is not on PATH")
        args = [executable, "--die-with-parent", "--new-session", "--unshare-all"]
        if self.mapped_ids:
            args += ["--unshare-user", "--cap-add", "ALL"]
        if self.share_network:
            args.append("--share-net")
        return args + [
            "--hostname",
            self.hostname,
            "--uid",
            "0" if self.mapped_ids else str(uid),
            "--gid",
            "0" if self.mapped_ids else str(gid),
        ]

    def _layout_args(self) -> list[str]:
        args = ["--bind", str(self.root), "/", "--proc", "/proc", "--dev", "/dev"]
        for path in self.closure:
            args += ["--ro-bind", path, path]
        args += ["--ro-bind", str(self.environment / "bin"), "/usr/bin"]
        if self.mapped_ids:
            bootstrap = Path(__file__).with_name("enter.py").resolve()
            args += ["--ro-bind", str(bootstrap), "/.harbor-init.py"]
        if self.transfer:
            args += ["--tmpfs", str(TRANSFER_DIR)]
        for bind in self.binds:
            args += _bind_args(bind)
        return args


def _bind_args(bind: Bind) -> list[str]:
    option = "--bind" if bind.writable else "--ro-bind"
    return [option, str(bind.source), str(bind.target)]


def _process_args(
    argv: Sequence[str], workdir: str, environment: Mapping[str, str]
) -> list[str]:
    args = ["--clearenv"]
    for name, value in environment.items():
        args += ["--setenv", name, value]
    return args + ["--chdir", workdir, "--", *argv]


def prepare_root(
    root: Path,
    *,
    hostname: str,
    workdirs: Iterable[str],
    resolv_conf: Path | None,
    rootfs: Path | None,
) -> None:
    """Create the persistent file tree that the sandbox starts from.

    A ``rootfs`` directory in the Nix result gives files at fixed paths, as the
    files of a Docker image do. Its links are followed, so the copies are real
    files that the sandbox user owns and can change.
    """
    root.mkdir(mode=0o755)
    if rootfs is not None and rootfs.is_dir():
        shutil.copytree(rootfs, root, symlinks=False, dirs_exist_ok=True)
        _make_owner_writable(root)
    for name, mode in (
        ("etc", 0o755),
        ("logs", 0o755),
        ("nix", 0o755),
        ("nix/store", 0o755),
        ("root", 0o700),
        ("run", 0o755),
        ("tmp", 0o1777),
        ("usr", 0o755),
        ("usr/bin", 0o755),
        ("var", 0o755),
        ("var/lib", 0o755),
    ):
        directory = root / name
        if not directory.is_dir():
            directory.mkdir(parents=True)
            directory.chmod(mode)
    (root / TRANSFER_DIR.name).mkdir(mode=0o755, exist_ok=True)
    if not (root / "bin").exists() and not (root / "bin").is_symlink():
        (root / "bin").symlink_to("usr/bin")
    hosts = f"127.0.0.1 localhost {hostname}\n::1 localhost {hostname}\n"
    for name, content in (
        ("etc/passwd", PASSWD),
        ("etc/group", GROUP),
        ("etc/hosts", hosts),
    ):
        if not (root / name).exists():
            (root / name).write_text(content)
    if resolv_conf is not None and resolv_conf.is_file():
        shutil.copyfile(resolv_conf, root / "etc/resolv.conf")
    for workdir in workdirs:
        (root / sandbox_relative(workdir)).mkdir(parents=True, exist_ok=True)


def _make_owner_writable(root: Path) -> None:
    """Add owner write access. Nix store files are read-only, Docker layers are not."""
    for directory, _, files in os.walk(root):
        Path(directory).chmod(Path(directory).stat().st_mode | 0o700)
        for name in files:
            path = Path(directory, name)
            path.chmod(path.stat().st_mode | 0o200)


def sandbox_relative(path: str) -> Path:
    """Map an absolute sandbox path to a path relative to the root."""
    pure = PurePosixPath(path)
    if not pure.is_absolute() or ".." in pure.parts:
        raise ValueError(f"sandbox path must be absolute without '..': {path}")
    return Path(*pure.parts[1:])


def remove_root(path: Path) -> None:
    """Delete a stopped sandbox tree, including directories without write access.

    No sandbox process runs after stop, so the tree cannot change during the walk.
    Symbolic links are never followed: a link can point at a host path.
    """
    path.chmod(0o700)
    for directory, subdirectories, _ in os.walk(path):
        for name in subdirectories:
            child = Path(directory, name)
            if not child.is_symlink():
                child.chmod(0o700)
    shutil.rmtree(path)
