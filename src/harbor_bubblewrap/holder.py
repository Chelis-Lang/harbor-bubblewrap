"""One long-lived bubblewrap sandbox and the commands that run inside it."""

from __future__ import annotations

import asyncio
import json
import os
import select
import signal
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path

from .idmap import IdMap
from .process import Completed, kill, run
from .sandbox import PATH, Bind, Sandbox

ENTER = Path(__file__).with_name("enter.py")
# Bound on bubblewrap setup. A healthy setup ends in milliseconds.
SETUP_SECONDS = 30.0
LOG_TAIL_BYTES = 4000


class Holder:
    """A bwrap process that keeps the namespaces of one service alive.

    The holder runs the service command. Later commands join its namespaces
    through ``enter.py``, so their processes, files and network persist, as in
    a container. When the service command exits, the sandbox ends.
    """

    def __init__(self, sandbox: Sandbox) -> None:
        self.sandbox = sandbox
        self.pid: int | None = None
        self._pidfd: int | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._output: asyncio.Task[None] | None = None
        self._tail = bytearray()
        self._stop_lock = asyncio.Lock()
        self._exec_pidfds: set[int] = set()

    @property
    def running(self) -> bool:
        if self._process is None or self._process.returncode is not None:
            return False
        return self._pidfd is None or not select.select([self._pidfd], [], [], 0)[0]

    @property
    def returncode(self) -> int | None:
        return None if self._process is None else self._process.returncode

    def identity(self) -> dict:
        if self.pid is None or self._pidfd is None:
            raise RuntimeError("the sandbox has no live namespace identity")
        signal.pidfd_send_signal(self._pidfd, 0)
        status = Path(f"/proc/{self.pid}/stat").read_text()
        root = self.sandbox.root.stat()
        return {
            "holder_pid": self.pid,
            "holder_start_time": int(status.rsplit(")", 1)[1].split()[19]),
            "root_device": root.st_dev,
            "root_inode": root.st_ino,
            "namespaces": {
                name: os.stat(f"/proc/{self.pid}/ns/{name}").st_ino
                for name in ("pid", "mnt", "user", "net", "ipc", "uts")
            },
        }

    async def start(
        self, argv: Sequence[str], *, workdir: str, environment: Mapping[str, str]
    ) -> None:
        read, write = os.pipe()
        mapping = IdMap.current() if self.sandbox.mapped_ids else None
        block_read, block_write = os.pipe() if mapping else (None, None)
        try:
            with (
                os.fdopen(read, "rb", buffering=0) as info_pipe,
                os.fdopen(write, "wb", buffering=0) as info_write,
            ):
                command = self.sandbox.holder_command(
                    argv,
                    workdir=workdir,
                    environment=environment,
                    info_fd=write,
                    block_fd=block_read,
                )
                self._process = await asyncio.create_subprocess_exec(
                    *command,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    pass_fds=tuple(fd for fd in (write, block_read) if fd is not None),
                    start_new_session=True,
                    env={},
                )
                info_write.close()
                self._output = asyncio.create_task(self._read_output())
                reader = asyncio.StreamReader()
                transport, _ = await asyncio.get_running_loop().connect_read_pipe(
                    lambda: asyncio.StreamReaderProtocol(reader), info_pipe
                )
                try:
                    info = await asyncio.wait_for(_read_info(reader), SETUP_SECONDS)
                finally:
                    transport.close()
                if info is None:
                    await self._process.wait()
                    await self._output
                    raise RuntimeError(f"bubblewrap setup failed: {self.log_tail()}")
                self.pid = int(info["child-pid"])
                self._pidfd = os.pidfd_open(self.pid)
                if mapping is not None:
                    await mapping.install(self.pid, SETUP_SECONDS)
                    os.write(block_write, b"1")
                await self._wait_for_root()
        except TimeoutError as error:
            await self.stop()
            raise RuntimeError(
                f"bubblewrap setup exceeded {SETUP_SECONDS} seconds: {self.log_tail()}"
            ) from error
        except BaseException:
            await self.stop()
            raise
        finally:
            for descriptor in (block_read, block_write):
                if descriptor is not None:
                    os.close(descriptor)

    async def _read_output(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        while chunk := await self._process.stdout.read(65536):
            self._tail.extend(chunk)
            del self._tail[:-LOG_TAIL_BYTES]

    async def _wait_for_root(self) -> None:
        """Wait until the sandbox process uses the sandbox root."""
        expected = self.sandbox.root.stat()
        deadline = time.monotonic() + SETUP_SECONDS
        while True:
            if not self.running:
                raise RuntimeError(f"the sandbox ended during setup: {self.log_tail()}")
            try:
                actual = os.stat(f"/proc/{self.pid}/root")
            except OSError:
                actual = None
            if actual is not None and (actual.st_dev, actual.st_ino) == (
                expected.st_dev,
                expected.st_ino,
            ):
                signal.pidfd_send_signal(self._pidfd, 0)
                return
            if time.monotonic() > deadline:
                raise RuntimeError(
                    f"the sandbox root did not appear: {self.log_tail()}"
                )
            await asyncio.sleep(0.01)

    async def exec(
        self,
        argv: Sequence[str],
        *,
        uid: int,
        gid: int,
        cwd: str,
        environment: Mapping[str, str],
        timeout_sec: float | None,
        stdin_data: bytes | None = None,
        started: asyncio.Future[int] | None = None,
        on_output=None,
    ) -> Completed:
        """Run ``argv`` inside the sandbox, as ``runc exec`` does in a container."""
        if not self.running:
            raise RuntimeError(f"the sandbox is not running: {self.log_tail()}")
        descriptor = os.dup(self._pidfd)
        request = {
            "pid": self.pid,
            "pidfd": descriptor,
            "uid": uid,
            "gid": gid,
            "cwd": cwd,
            "env": dict(environment),
            "argv": list(argv),
            "mapped_ids": self.sandbox.mapped_ids,
            "capabilities": self.sandbox.capabilities if uid == 0 else 0,
            "no_new_privs": self.sandbox.no_new_privs,
        }
        with tempfile.TemporaryFile() as source:
            if stdin_data is not None:
                source.write(stdin_data)
                source.seek(0)
                request["stdin_fd"] = source.fileno()
            start_read, start_write = os.pipe() if started is not None else (None, None)
            if start_write is not None:
                request["started_fd"] = start_write
            descriptors = tuple(
                fd
                for fd in (descriptor, source.fileno(), start_write)
                if fd is not None
            )
            notification = (
                asyncio.create_task(_notify_start(start_read, started))
                if started is not None
                else None
            )
            try:
                task = asyncio.create_task(
                    run(
                        [sys.executable, "-I", "-B", str(ENTER)],
                        timeout_sec,
                        stdin_data=json.dumps(request).encode(),
                        env={},
                        pass_fds=descriptors,
                        on_output=on_output,
                        on_failure=self.stop if self.sandbox.mapped_ids else None,
                        active_pidfds=self._exec_pidfds,
                    )
                )
                result = await task
                if started is not None and not started.done() and result.return_code:
                    started.set_exception(
                        RuntimeError(
                            f"sandbox command failed with exit {result.return_code}: "
                            + (result.stderr or "")
                        )
                    )
                return result
            finally:
                os.close(descriptor)
                if start_write is not None:
                    os.close(start_write)
                if notification is not None:
                    await notification

    async def transfer(
        self, argv: Sequence[str], binds: Sequence[Bind], timeout_sec: float
    ) -> Completed:
        if not self.sandbox.mapped_ids:
            return await run(
                self.sandbox.transfer_command(argv, binds), timeout_sec, env={}
            )
        sandbox = replace(
            self.sandbox,
            binds=self.sandbox.binds + tuple(binds),
            uid=0,
            gid=0,
            capabilities=(1 << 0) | (1 << 1) | (1 << 3),
            no_new_privs=True,
            transfer=True,
        )
        holder = Holder(sandbox)
        try:
            await holder.start(
                ["/usr/bin/sleep", "infinity"],
                workdir="/",
                environment={"PATH": PATH},
            )
            return await holder.exec(
                argv,
                uid=0,
                gid=0,
                cwd="/",
                environment={"PATH": PATH},
                timeout_sec=timeout_sec,
            )
        finally:
            await holder.stop()

    async def stop(self) -> None:
        """End the sandbox and wait until its PID namespace is empty."""
        async with self._stop_lock:
            commands = []
            try:
                for descriptor in self._exec_pidfds:
                    commands.append(os.dup(descriptor))
                # Retire relays before namespace teardown, without an output ACK.
                for descriptor in commands:
                    try:
                        signal.pidfd_send_signal(descriptor, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                if self._process is not None:
                    await kill(self._process)
                for descriptor in commands:
                    await _wait_for_exit(descriptor)
                descriptor, self._pidfd = self._pidfd, None
                if descriptor is not None:
                    try:
                        await _wait_for_exit(descriptor)
                    finally:
                        os.close(descriptor)
                if self._output is not None:
                    await self._output
            finally:
                for descriptor in commands:
                    os.close(descriptor)

    def log_tail(self) -> str:
        return self._tail.decode(errors="replace")


async def _notify_start(descriptor: int, started: asyncio.Future[int]) -> None:
    reader = asyncio.StreamReader()
    with os.fdopen(descriptor, "rb", buffering=0) as source:
        transport, _ = await asyncio.get_running_loop().connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(reader), source
        )
        try:
            info = await _read_info(reader)
            if not started.done():
                if info is None or "pid" not in info:
                    started.set_exception(RuntimeError("sandbox command did not start"))
                else:
                    started.set_result(int(info["pid"]))
        finally:
            transport.close()


async def _read_info(reader: asyncio.StreamReader) -> dict | None:
    """Read the --info-fd document of bwrap. Return None at end of file."""
    data = b""
    while chunk := await reader.read(4096):
        data += chunk
        try:
            return json.loads(data)
        except ValueError:
            continue
    return None


async def _wait_for_exit(descriptor: int) -> None:
    loop = asyncio.get_running_loop()
    exited = loop.create_future()

    def ready() -> None:
        if not exited.done():
            exited.set_result(None)

    loop.add_reader(descriptor, ready)
    try:
        await exited
    finally:
        loop.remove_reader(descriptor)
