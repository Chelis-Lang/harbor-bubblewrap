"""Run a host program with a deadline. On timeout or cancellation, kill its group."""

from __future__ import annotations

import asyncio
import codecs
import os
import signal
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

OUTPUT_CHUNK_BYTES = 8192


@dataclass(frozen=True)
class Completed:
    return_code: int
    stdout: str | None
    stderr: str | None


async def run(
    argv: Sequence[str],
    timeout_sec: float | None,
    *,
    stdin_data: bytes | None = None,
    env: Mapping[str, str] | None = None,
    pass_fds: Sequence[int] = (),
    on_output=None,
    on_failure=None,
    active_pidfds: set[int] | None = None,
) -> Completed:
    deadline = None if timeout_sec is None else time.monotonic() + timeout_sec
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL
        if stdin_data is None
        else asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
        env=env,
        pass_fds=pass_fds,
    )
    descriptor = None
    try:
        duration = None if deadline is None else max(0, deadline - time.monotonic())
        timeout = asyncio.timeout(duration)
        try:
            async with timeout:
                if active_pidfds is not None:
                    try:
                        descriptor = os.pidfd_open(process.pid)
                    except ProcessLookupError:
                        pass
                    if descriptor is not None:
                        active_pidfds.add(descriptor)
                stdout, stderr = await (
                    process.communicate(stdin_data)
                    if on_output is None
                    else _stream(process, stdin_data, on_output)
                )
        except BaseException as original:

            async def cleanup(cause):
                host_cleanup = asyncio.create_task(kill(process, drain=True))
                try:
                    if on_failure is not None:
                        try:
                            await on_failure()
                        except BaseException as failure:  # noqa: BLE001 - retain sandbox cleanup failure and still reap the host command.
                            cause.add_note("sandbox cleanup failed: " + repr(failure))
                finally:
                    await host_cleanup

            try:
                cleanup_task = asyncio.create_task(cleanup(original))
                while not cleanup_task.done():
                    try:
                        await asyncio.shield(cleanup_task)
                    except asyncio.CancelledError:
                        continue
                cleanup_task.result()
            except BaseException as cleanup:  # noqa: BLE001 - retain cleanup failure without replacing the original cause.
                original.add_note("command cleanup failed: " + repr(cleanup))
            if isinstance(original, TimeoutError) and timeout.expired():
                raise RuntimeError(
                    f"Command timed out after {timeout_sec} seconds"
                ) from original
            raise
        return Completed(process.returncode, _text(stdout), _text(stderr))
    finally:
        if descriptor is not None:
            active_pidfds.discard(descriptor)
            os.close(descriptor)


async def _stream(process, stdin_data, on_output):
    async def send_input():
        if stdin_data is not None:
            try:
                process.stdin.write(stdin_data)
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                process.stdin.close()
            try:
                await process.stdin.wait_closed()
            except (BrokenPipeError, ConnectionResetError):
                pass

    async def collect(reader, stream):
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        pieces = []
        while True:
            chunk = await reader.read(OUTPUT_CHUNK_BYTES)
            text = decoder.decode(chunk, final=not chunk)
            if text:
                await on_output(text, stream)
                pieces.append(text.encode())
            if not chunk:
                return b"".join(pieces)

    tasks = [
        asyncio.create_task(send_input()),
        asyncio.create_task(collect(process.stdout, "stdout")),
        asyncio.create_task(collect(process.stderr, "stderr")),
        asyncio.create_task(process.wait()),
    ]
    try:
        _, stdout, stderr, _ = await asyncio.gather(*tasks)
        return stdout, stderr
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def _text(data: bytes) -> str | None:
    return data.decode(errors="replace") if data else None


async def kill(process: asyncio.subprocess.Process, *, drain: bool = False) -> None:
    """Kill the process group of ``process`` and wait for the process."""
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    if drain:

        async def discard(reader):
            if reader is not None:
                while await reader.read(OUTPUT_CHUNK_BYTES):
                    pass

        await asyncio.gather(
            discard(process.stdout), discard(process.stderr), process.wait()
        )
    else:
        await process.wait()
