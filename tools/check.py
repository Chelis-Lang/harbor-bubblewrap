"""Run `python3.12 tools/check.py` to check the core distribution.

The command needs Python 3.12 or later and access to PyPI.
It installs the pinned development tools in a temporary environment.
It checks Python code and distribution metadata, then tests an installed wheel.
The build frontend creates the wheel from the source distribution.
Tests run outside the checkout without Harbor or Linux namespace tools.
The command copies the checked distributions to dist/.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import tomllib
import venv
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    with (root / "pyproject.toml").open("rb") as stream:
        project = tomllib.load(stream)["project"]
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    environment.pop("PYTHONHOME", None)
    environment["HELL_TEST_NAMESPACES"] = "0"

    def run(*command: str | Path, cwd: Path = root) -> None:
        subprocess.run(command, cwd=cwd, env=environment, check=True)

    with tempfile.TemporaryDirectory(prefix="harbor-bubblewrap-check-") as directory:
        temporary = Path(directory)
        tools = temporary / "tools"
        venv.EnvBuilder(with_pip=True).create(tools)
        tool_python = tools / "bin" / "python"
        run(
            tool_python,
            "-m",
            "pip",
            "install",
            *project["optional-dependencies"]["dev"],
        )
        run(tool_python, "-m", "ruff", "check", "src", "tests", "tools")
        run(tool_python, "-m", "ruff", "format", "--check", "src", "tests", "tools")
        distributions = temporary / "dist"
        run(tool_python, "-m", "build", "--outdir", distributions)
        wheel = next(distributions.glob("*.whl"))
        source = next(distributions.glob("*.tar.gz"))
        run(tool_python, "-m", "twine", "check", "--strict", source, wheel)

        core = temporary / "core"
        venv.EnvBuilder(with_pip=True).create(core)
        core_python = core / "bin" / "python"
        run(core_python, "-m", "pip", "install", f"{wheel}[test]")
        run(core_python, "-m", "pip", "check", cwd=temporary)
        run(
            core_python,
            "-I",
            "-c",
            """
import importlib.util
import sys
from importlib.metadata import version
from pathlib import Path

import harbor_bubblewrap

assert importlib.util.find_spec("harbor") is None
assert not Path(harbor_bubblewrap.__file__).resolve().is_relative_to(Path(sys.argv[1]))
assert version("harbor-bubblewrap") == sys.argv[2]
""",
            root,
            project["version"],
            cwd=temporary,
        )
        tests = temporary / "tests"
        shutil.copytree(
            root / "tests",
            tests,
            ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"),
        )
        run(core_python, "-I", "-m", "pytest", "-q", tests, cwd=temporary)
        output = root / "dist"
        output.mkdir(exist_ok=True)
        for distribution in (source, wheel):
            shutil.copy2(distribution, output / distribution.name)


if __name__ == "__main__":
    main()
