"""Command errors retain their cause and do not leave host processes alive."""

import asyncio
import os
import sys

import pytest
from harbor_bubblewrap.process import run


def test_sandbox_cleanup_error_cannot_skip_host_process_retirement():
    async def scenario():
        original = ValueError("original output guard")
        cleanup_error = OSError("sandbox cleanup refused")
        output = ""

        async def reject(text, stream):
            nonlocal output
            assert stream == "stdout"
            output += text
            if "\n" in output:
                raise original

        async def refuse_cleanup():
            raise cleanup_error

        with pytest.raises(ValueError) as caught:
            await run(
                [
                    sys.executable,
                    "-I",
                    "-c",
                    (
                        "import os, signal\n"
                        "print(os.getpid(), flush=True)\n"
                        "signal.pause()\n"
                    ),
                ],
                5,
                on_output=reject,
                on_failure=refuse_cleanup,
            )
        assert caught.value is original
        assert str(cleanup_error) in "\n".join(original.__notes__)
        with pytest.raises(ProcessLookupError):
            os.kill(int(output), 0)

    asyncio.run(scenario())
