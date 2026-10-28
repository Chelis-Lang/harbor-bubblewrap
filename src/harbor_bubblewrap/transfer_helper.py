"""Trusted copy program for one Harbor transfer inside bubblewrap.

The host starts this program with its own Python interpreter and a read-only
host root. The writable sandbox tree arrives as data at ``/.harbor-seat``, so
no loader configuration, module or program from that tree can affect this
process. Every path that this program opens stays inside the seat root: an
absolute link resolves from the seat root, and ``..`` above that root stops
at the root. A final file link is followed. Each opened object receives a type
check before any data read.

This file runs as ``python3 -I -B transfer_helper.py --request <json>``. It
imports only the standard library.
"""

from __future__ import annotations

import json
import os
import posixpath
import stat
import sys
from collections import deque

MAX_LINK_HOPS = 40
FILE_MODE = 0o600
DIRECTORY_MODE = 0o700
CHUNK_BYTES = 1 << 20


class Refused(Exception):
    """A transfer entry failed a fixed safety rule."""


def _open(anchor: int, name: str, flags: int) -> int:
    return os.open(name, flags | os.O_CLOEXEC, dir_fd=anchor)


def _reopen(fd: int, flags: int) -> int:
    """Open the pinned object again, without a new path resolution."""
    # The procfs name is itself a link, so O_NOFOLLOW must not join these flags.
    return os.open(f"/proc/self/fd/{fd}", flags | os.O_CLOEXEC & ~os.O_NOFOLLOW)


def _close(descriptors) -> None:
    for descriptor in descriptors:
        os.close(descriptor)


def _resolve(root: int, base: int, path: str) -> tuple[int, list[int]]:
    """Resolve ``path`` inside the seat root and return the final descriptor.

    ``base`` anchors a relative path. An absolute path or an absolute link
    restarts at the seat root. The caller closes every descriptor in the keep
    list and the final descriptor.
    """
    stack = [os.dup(root)]
    pending = deque(path.split("/"))
    if not path.startswith("/"):
        stack.append(os.dup(base))
    hops = 0
    try:
        while pending:
            name = pending.popleft()
            if name in {"", "."}:
                continue
            if name == "..":
                if len(stack) > 1:
                    os.close(stack.pop())
                continue
            parent = stack[-1]
            descriptor = _open(parent, name, os.O_PATH | os.O_NOFOLLOW)
            if stat.S_ISLNK(os.fstat(descriptor).st_mode):
                hops += 1
                if hops > MAX_LINK_HOPS:
                    raise Refused(f"too many links while resolving {path}")
                # The procfs name of a pinned link resolves to a host path, so
                # read the link name beside its parent descriptor.
                target = os.readlink(name, dir_fd=parent)
                os.close(descriptor)
                if target.startswith("/"):
                    while len(stack) > 1:
                        os.close(stack.pop())
                pending = deque(target.split("/")) + pending
                continue
            stack.append(descriptor)
    except BaseException:
        _close(stack)
        raise
    final = stack.pop()
    return final, stack


def _require(mode: int, predicate, what: str, path: str) -> None:
    if not predicate(mode):
        raise Refused(f"{path} is not {what}")


def _is_regular(mode: int) -> bool:
    return stat.S_ISREG(mode)


def _is_directory(mode: int) -> bool:
    return stat.S_ISDIR(mode)


def _copy_bytes(source: int, destination: int) -> None:
    while chunk := os.read(source, CHUNK_BYTES):
        view = memoryview(chunk)
        while view:
            written = os.write(destination, view)
            view = view[written:]


def _pinned_regular(path: str, resolved: int) -> int:
    """Return a descriptor to the pinned regular file, before any data read."""
    info = os.fstat(resolved)
    _require(info.st_mode, _is_regular, "a regular file", path)
    descriptor = _reopen(resolved, os.O_RDONLY | os.O_NONBLOCK)
    again = os.fstat(descriptor)
    if (again.st_dev, again.st_ino) != (info.st_dev, info.st_ino):
        os.close(descriptor)
        raise Refused(f"{path} changed while it was resolved")
    _require(again.st_mode, _is_regular, "a regular file", path)
    return descriptor


def _pinned_directory(path: str, resolved: int) -> int:
    info = os.fstat(resolved)
    _require(info.st_mode, _is_directory, "a directory", path)
    descriptor = _reopen(resolved, os.O_RDONLY | os.O_DIRECTORY)
    again = os.fstat(descriptor)
    if (again.st_dev, again.st_ino) != (info.st_dev, info.st_ino):
        os.close(descriptor)
        raise Refused(f"{path} changed while it was resolved")
    _require(again.st_mode, _is_directory, "a directory", path)
    return descriptor


def _write_file(
    directory: int, name: str, source: int, *, replace: bool, mode: int = FILE_MODE
) -> None:
    """Publish one regular file in ``directory``, through no links."""
    try:
        existing = _open(directory, name, os.O_PATH | os.O_NOFOLLOW)
    except FileNotFoundError:
        pass
    else:
        try:
            _require(
                os.fstat(existing).st_mode,
                _is_regular,
                "an existing file to replace",
                name,
            )
        finally:
            os.close(existing)
        if not replace:
            raise Refused(f"{name} is an existing destination entry")
        os.unlink(name, dir_fd=directory)
    target = _open(
        directory, name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    )
    os.fchmod(target, mode)
    try:
        _copy_bytes(source, target)
    finally:
        os.close(target)


def _make_directory(directory: int, name: str) -> int:
    try:
        os.mkdir(name, DIRECTORY_MODE, dir_fd=directory)
    except FileExistsError:
        pass
    descriptor = _open(directory, name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    _require(os.fstat(descriptor).st_mode, _is_directory, "a directory", name)
    return descriptor


def _inspect_directory(root: int, source: str, output: int) -> None:
    try:
        resolved, keep = _resolve(root, root, source)
    except (FileNotFoundError, NotADirectoryError):
        directory = False
    else:
        try:
            directory = stat.S_ISDIR(os.fstat(resolved).st_mode)
        finally:
            _close([resolved, *keep])
    descriptor = _open(
        output, "file", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    )
    with os.fdopen(descriptor, "wb") as stream:
        os.fchmod(stream.fileno(), FILE_MODE)
        stream.write(b"true" if directory else b"false")


def _download_file(root: int, source: str, output: int) -> None:
    resolved, keep = _resolve(root, root, source)
    try:
        contents = _pinned_regular(source, resolved)
    finally:
        _close([resolved, *keep])
    try:
        _write_file(output, "file", contents, replace=False)
    finally:
        os.close(contents)


def _copy_download_tree(directory: int, output: int, where: str) -> None:
    for name in sorted(os.listdir(directory)):
        path = posixpath.join(where, name)
        entry = _open(directory, name, os.O_PATH | os.O_NOFOLLOW)
        try:
            mode = os.fstat(entry).st_mode
            if stat.S_ISLNK(mode):
                raise Refused(f"the download of {path} is a symbolic link")
            if stat.S_ISDIR(mode):
                child = _pinned_directory(path, entry)
                try:
                    target = _make_directory(output, name)
                    try:
                        _copy_download_tree(child, target, path)
                    finally:
                        os.close(target)
                finally:
                    os.close(child)
            elif stat.S_ISREG(mode):
                contents = _pinned_regular(path, entry)
                try:
                    _write_file(output, name, contents, replace=True)
                finally:
                    os.close(contents)
            else:
                raise Refused(f"the download of {path} is not a regular file")
        finally:
            os.close(entry)


def _download_dir(root: int, source: str, output: int) -> None:
    resolved, keep = _resolve(root, root, source)
    try:
        directory = _pinned_directory(source, resolved)
    finally:
        _close([resolved, *keep])
    try:
        _copy_download_tree(directory, output, source)
    finally:
        os.close(directory)


def _destination_directory(seat: int, target: str, *, create: bool) -> int:
    """Resolve or create the parent directory of ``target`` inside the root."""
    parent = posixpath.dirname(target) or "/"
    names = [name for name in parent.split("/") if name not in {"", "."}]
    current = os.dup(seat)
    try:
        for name in names:
            if name == "..":
                raise Refused(f"upload target escapes the seat root: {target}")
            try:
                following = _open(
                    current, name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                )
            except FileNotFoundError:
                if not create:
                    raise Refused(f"upload parent {parent} is absent") from None
                os.mkdir(name, DIRECTORY_MODE, dir_fd=current)
                following = _open(
                    current, name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                )
            except NotADirectoryError:
                raise Refused(f"upload parent {parent} is not a directory") from None
            os.close(current)
            current = following
        return current
    except BaseException:
        os.close(current)
        raise


def _upload_file(seat: int, source: int, target: str) -> None:
    contents = _pinned_regular("upload source", source)
    parent = _destination_directory(seat, target, create=True)
    try:
        _write_file(
            parent,
            posixpath.basename(target),
            contents,
            replace=True,
            mode=os.fstat(contents).st_mode & 0o777,
        )
    finally:
        os.close(contents)
        os.close(parent)


def _copy_upload_tree(source: int, seat: int, where: str) -> None:
    for name in sorted(os.listdir(source)):
        child = _open(source, name, os.O_PATH | os.O_NOFOLLOW)
        try:
            mode = os.fstat(child).st_mode
            if stat.S_ISDIR(mode):
                directory = _pinned_directory(where, child)
                try:
                    target = _make_directory(seat, name)
                    try:
                        _copy_upload_tree(
                            directory, target, posixpath.join(where, name)
                        )
                    finally:
                        os.close(target)
                finally:
                    os.close(directory)
            elif stat.S_ISREG(mode):
                contents = _pinned_regular(name, child)
                try:
                    _write_file(seat, name, contents, replace=True, mode=mode & 0o777)
                finally:
                    os.close(contents)
            else:
                raise Refused(f"the upload of {name} is not a regular file")
        finally:
            os.close(child)


def _upload_dir(seat: int, source: int, target: str) -> None:
    _require(os.fstat(source).st_mode, _is_directory, "a directory", "upload source")
    destination = _destination_directory(seat, posixpath.join(target, "x"), create=True)
    try:
        _copy_upload_tree(source, destination, target)
    finally:
        os.close(destination)


MODES = {
    "is-dir": _inspect_directory,
    "download-file": _download_file,
    "download-dir": _download_dir,
    "upload-file": _upload_file,
    "upload-dir": _upload_dir,
}


def _transfer_path(path: str) -> int:
    """Open one host bind outside the persistent seat tree."""
    resolved = os.open(path, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(resolved)
        _require(
            info.st_mode,
            lambda mode: stat.S_ISREG(mode) or stat.S_ISDIR(mode),
            "a file or directory",
            path,
        )
        following = _reopen(resolved, os.O_RDONLY | os.O_NONBLOCK)
    finally:
        os.close(resolved)
    _require(
        os.fstat(following).st_mode,
        lambda mode: stat.S_ISREG(mode) or stat.S_ISDIR(mode),
        "a file or directory",
        path,
    )
    return following


def main(request: dict) -> None:
    mode = request["mode"]
    if mode not in MODES:
        raise Refused(f"unknown transfer mode {mode!r}")
    seat = os.open(request["seat"], os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        if mode.startswith("download-") or mode == "is-dir":
            output = _transfer_path(request["output"])
            try:
                MODES[mode](seat, request["source"], output)
            finally:
                os.close(output)
        else:
            source = _transfer_path(request["source"])
            try:
                MODES[mode](seat, source, request["target"])
            finally:
                os.close(source)
    finally:
        os.close(seat)


if __name__ == "__main__":
    try:
        main(json.loads(sys.argv[sys.argv.index("--request") + 1]))
    except (Refused, OSError) as error:
        print(f"transfer refused: {error}", file=sys.stderr)
        raise SystemExit(1) from error
