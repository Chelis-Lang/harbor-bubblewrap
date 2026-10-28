"""Trusted transfer plumbing for downloads and uploads.

A transfer never executes a program from the writable sandbox tree, and the
host publishes a download through fresh regular files and directories. This
module owns both the copy process request and the host publication steps.

The caller selects the host destination, so its ancestor directories may
contain links and the walk follows them. Below that selected destination, no
link is followed: an existing file entry is unlinked and replaced by a fresh
inode, and an existing directory link is refused.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .sandbox import TRANSFER_OUTPUT, TRANSFER_SOURCE, TRUSTED_SEAT, Bind

# A transfer of a large output tree finishes well inside this bound. The bound
# exists so a blocked source cannot hold the host process forever.
TRANSFER_SECONDS = 300.0
FILE_MODE = 0o644
DIRECTORY_MODE = 0o755
CHUNK_BYTES = 1 << 20


@dataclass(frozen=True)
class Transfer:
    """One copy request between the host and one sandbox.

    ``source`` is a sandbox path for a download and a bound host path for an
    upload. ``target`` is a sandbox path for an upload. Exactly one of
    ``output`` and ``input`` is set, and it is the host directory bind.
    """

    mode: str
    source: str
    target: str = "/"
    output: Path | None = None
    input: Path | None = None

    def bind(self) -> Bind:
        if self.output is not None:
            return Bind(self.output, PurePosixPath(TRANSFER_OUTPUT), writable=True)
        if self.input is not None:
            return Bind(self.input, PurePosixPath(TRANSFER_SOURCE), writable=False)
        raise ValueError("transfer needs a host input or output directory")

    def payload(self) -> dict:
        if self.output is not None:
            return {
                "mode": self.mode,
                "seat": TRUSTED_SEAT,
                "source": self.source,
                "output": TRANSFER_OUTPUT,
            }
        return {
            "mode": self.mode,
            "seat": TRUSTED_SEAT,
            "source": TRANSFER_SOURCE,
            "target": self.target,
        }


def _open_directory(path: Path | str, anchor: int | None = None) -> int:
    return os.open(
        path,
        os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC,
        dir_fd=anchor,
    )


def _copy_bytes(source: int, destination: int) -> None:
    while chunk := os.read(source, CHUNK_BYTES):
        view = memoryview(chunk)
        while view:
            written = os.write(destination, view)
            view = view[written:]


def _pinned_regular(path: Path) -> int:
    """Open a staged regular file through no links, and prove it did not change."""
    before = os.lstat(path)
    if not stat.S_ISREG(before.st_mode):
        raise RuntimeError(f"The download of {path} is not a regular file")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    after = os.fstat(descriptor)
    if (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino):
        os.close(descriptor)
        raise RuntimeError(f"The download of {path} changed during publication")
    if not stat.S_ISREG(after.st_mode):
        os.close(descriptor)
        raise RuntimeError(f"The download of {path} is not a regular file")
    return descriptor


def _walker(anchor: int, name: str, *, create: bool) -> int:
    """Open one destination component. The caller owns this directory."""
    try:
        return _open_directory(name, anchor)
    except FileNotFoundError:
        if not create:
            raise RuntimeError(f"The host destination {name} is absent") from None
        os.mkdir(name, DIRECTORY_MODE, dir_fd=anchor)
        return _open_directory(name, anchor)
    except OSError as error:
        raise RuntimeError(
            f"The host destination {name} is not a directory: {error}"
        ) from error


def _selected_directory(anchor: int, name: str, where: Path) -> int:
    """Open one directory inside the selected destination, through no links."""
    try:
        return os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=anchor,
        )
    except FileNotFoundError:
        os.mkdir(name, DIRECTORY_MODE, dir_fd=anchor)
        return os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=anchor,
        )
    except OSError as error:
        raise RuntimeError(
            f"The host destination {where} is not a directory: {error}"
        ) from error


def _destination_parent(target: Path, *, create: bool) -> int:
    """Open the parent directory of ``target``, component by component."""
    if not target.is_absolute() or target.name in {"", ".", ".."}:
        raise ValueError(f"host destination must be an absolute file path: {target}")
    current = _open_directory("/")
    try:
        for name in target.parent.parts[1:]:
            following = _walker(current, name, create=create)
            os.close(current)
            current = following
        return current
    except BaseException:
        os.close(current)
        raise


def _replace_file(directory: int, name: str, contents: int, where: Path) -> None:
    """Publish one fresh inode in ``directory`` and never write an existing one."""
    try:
        existing = os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        pass
    else:
        if stat.S_ISDIR(existing.st_mode):
            raise RuntimeError(f"The host destination {where} is a directory")
        os.unlink(name, dir_fd=directory)
    target = os.open(
        name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        mode=FILE_MODE,
        dir_fd=directory,
    )
    try:
        _copy_bytes(contents, target)
    finally:
        os.close(target)


def publish_file(staging: Path, target: Path) -> None:
    """Publish one staged file as a fresh host file."""
    contents = _pinned_regular(staging)
    try:
        parent = _destination_parent(target, create=True)
        try:
            _replace_file(parent, target.name, contents, target)
        finally:
            os.close(parent)
    finally:
        os.close(contents)


def publish_dir(staging: Path, target: Path) -> None:
    """Merge one staged tree into the host destination, with fresh entries."""
    if not stat.S_ISDIR(os.lstat(staging).st_mode):
        raise RuntimeError(f"The download of {staging} is not a directory")
    directory = os.open(staging, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        parent = _destination_parent(target, create=True)
        try:
            destination = _selected_directory(parent, target.name, target)
            try:
                _copy_tree(directory, destination, staging, target)
            finally:
                os.close(destination)
        finally:
            os.close(parent)
    finally:
        os.close(directory)


def _copy_tree(staging: int, destination: int, where: Path, root: Path) -> None:
    for name in sorted(os.listdir(staging)):
        entry = os.open(
            name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=staging,
        )
        try:
            mode = os.fstat(entry).st_mode
            if stat.S_ISDIR(mode):
                child = _selected_directory(destination, name, root / name)
                try:
                    _copy_tree(entry, child, where / name, root / name)
                finally:
                    os.close(child)
            elif stat.S_ISREG(mode):
                _replace_file(destination, name, entry, root / name)
            else:
                raise RuntimeError(f"The download of {where / name} is not regular")
        finally:
            os.close(entry)
