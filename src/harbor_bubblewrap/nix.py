"""Build a Nix definition and read the closure of the result."""

from __future__ import annotations

from pathlib import Path

from .process import run


async def build(definition: Path, out_link: Path, attribute: str | None = None) -> Path:
    """Build ``definition`` and return the store path of the result.

    The out link is a garbage-collector root for the life of the sandbox.
    """
    argv = ["nix-build", str(definition), "--out-link", str(out_link)]
    if attribute is not None:
        argv += ["--attr", attribute]
    result = await run(argv, None)
    paths = (result.stdout or "").split()
    if result.return_code != 0 or not paths:
        raise RuntimeError(
            f"{' '.join(argv)} failed with exit code {result.return_code}: "
            f"{result.stderr}"
        )
    return Path(paths[-1])


async def closure(path: Path) -> tuple[str, ...]:
    """Return the store paths that ``path`` needs, including itself."""
    argv = ["nix-store", "--query", "--requisites", str(path)]
    result = await run(argv, None)
    if result.return_code != 0:
        raise RuntimeError(
            f"{' '.join(argv)} failed with exit code {result.return_code}: "
            f"{result.stderr}"
        )
    return tuple((result.stdout or "").split())
