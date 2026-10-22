"""Test adapter contracts in real Linux namespaces through the Harbor SDK."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from importlib.resources import files
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("HELL_TEST_NAMESPACES") != "1",
    reason="the Linux namespace tier runs the bubblewrap backend",
)

DEFINITION = Path(
    str(files("harbor_bubblewrap").joinpath("tasks/hello-file/environment/default.nix"))
)
PROBE = """
import asyncio
import json
import sys
from pathlib import Path

from harbor.models.task.config import EnvironmentConfig
from harbor.models.trial.paths import TrialPaths
from harbor_bubblewrap.environment import BubblewrapEnvironment

async def main():
    directory, trial, options = sys.argv[1:]
    options = json.loads(options)
    actions = options.pop("actions", [])
    config = options.pop("config", {})
    try:
        environment = BubblewrapEnvironment(
            environment_dir=Path(directory),
            environment_name="contracts",
            session_id="contracts__env",
            trial_paths=TrialPaths(Path(trial)),
            task_env_config=EnvironmentConfig(**config),
            **options,
        )
    except ValueError as error:
        print(json.dumps({"error": type(error).__name__}))
        return
    await environment.start(force_build=False)
    results = []
    try:
        for action in actions:
            try:
                result = await environment.service_exec(**action)
                results.append({"code": result.return_code, "stdout": result.stdout or ""})
            except ValueError as error:
                results.append({"error": type(error).__name__})
    finally:
        await environment.stop(delete=True)
    print(json.dumps(results))

asyncio.run(main())
"""


def environment(tmp_path: Path, compose: str | None = None) -> Path:
    directory = tmp_path / "environment"
    directory.mkdir()
    if compose is None:
        definition = DEFINITION.read_text()
    else:
        definition = (
            "let tools = import "
            + json.dumps(str(DEFINITION.resolve()))
            + "; in { main = tools; side = tools; }\n"
        )
        (directory / "docker-compose.yaml").write_text(compose)
    (directory / "default.nix").write_text(definition)
    return directory


def probe(directory: Path, tmp_path: Path, **options):
    interpreter = sys.executable
    result = subprocess.run(
        [
            interpreter,
            "-c",
            PROBE,
            str(directory),
            str(tmp_path / "trial"),
            json.dumps(options),
        ],
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout.splitlines()[-1])


@pytest.mark.parametrize(
    ("task_workdir", "compose_workdir", "expected"),
    [
        ("/task", "/compose", "/task"),
        (None, "/compose", "/compose"),
        (None, None, "/image/relative"),
    ],
)
def test_main_workdir_precedence(tmp_path, task_workdir, compose_workdir, expected):
    spec = "services:\n  main:\n    build: .\n"
    if compose_workdir:
        spec += f"    working_dir: {compose_workdir}\n"
    directory = environment(tmp_path, spec)
    (directory / "Dockerfile").write_text(
        "FROM scratch\nWORKDIR /image\nWORKDIR relative\n"
    )
    result = probe(
        directory,
        tmp_path,
        config={"workdir": task_workdir},
        actions=[{"command": "pwd"}],
    )
    assert result == [{"code": 0, "stdout": expected + "\n"}]


def test_service_build_workdirs_and_main_fallback(tmp_path):
    directory = environment(
        tmp_path,
        "services:\n  main: {}\n  side:\n    build:\n"
        "      context: ./side\n      dockerfile: Customfile\n",
    )
    (directory / "Dockerfile").write_text("FROM scratch\nWORKDIR /main-image\n")
    (directory / "side").mkdir()
    (directory / "side/Customfile").write_text("FROM scratch\nWORKDIR /side-image\n")
    result = probe(
        directory,
        tmp_path,
        actions=[{"command": "pwd"}, {"command": "pwd", "service": "side"}],
    )
    assert result == [
        {"code": 0, "stdout": "/main-image\n"},
        {"code": 0, "stdout": "/side-image\n"},
    ]


def test_sidecar_without_build_does_not_use_main_workdir(tmp_path):
    directory = environment(tmp_path, "services:\n  main: {}\n  side: {}\n")
    (directory / "Dockerfile").write_text("FROM scratch\nWORKDIR /main-image\n")
    result = probe(directory, tmp_path, actions=[{"command": "pwd", "service": "side"}])
    assert result == [{"code": 0, "stdout": "/\n"}]


def test_user_requests_and_root_housekeeping_alias(tmp_path):
    directory = environment(
        tmp_path,
        "services:\n  main:\n    user: '1234:1234'\n  side:\n    user: '2345:2345'\n",
    )
    actions = []
    for service, uid in [("main", 1234), ("side", 2345)]:
        for user in [None, "root", uid, str(uid), uid + 1, "nobody", 0]:
            actions.append({"service": service, "command": "id -u", "user": user})
    result = probe(directory, tmp_path, actions=actions)
    for offset, uid in [(0, 1234), (7, 2345)]:
        assert result[offset : offset + 4] == [{"code": 0, "stdout": f"{uid}\n"}] * 4
        assert result[offset + 4 : offset + 7] == [{"error": "ValueError"}] * 3


@pytest.mark.parametrize("resource", ["cpu", "memory"])
@pytest.mark.parametrize("mode", ["limit", "request", "guarantee"])
def test_unsupported_resource_modes_fail(tmp_path, resource, mode):
    directory = environment(tmp_path)
    result = probe(
        directory,
        tmp_path,
        config={"cpus": 1, "memory_mb": 128},
        **{f"{resource}_enforcement_policy": mode},
    )
    assert result == {"error": "ValueError"}


def test_file_bind_keeps_file_type_and_read_only_mode(tmp_path):
    directory = environment(
        tmp_path,
        "services:\n  main:\n    volumes:\n      - ./input.txt:/input.txt:ro\n",
    )
    source = directory / "input.txt"
    source.write_text("original\n")
    result = probe(
        directory,
        tmp_path,
        actions=[
            {"command": "test -f /input.txt && cat /input.txt"},
            {"command": "printf changed > /input.txt"},
            {"command": "cat /input.txt"},
        ],
    )
    assert result[0] == {"code": 0, "stdout": "original\n"}
    assert result[1]["code"] != 0
    assert result[2] == {"code": 0, "stdout": "original\n"}
    assert source.is_file()
    assert source.read_text() == "original\n"


def test_main_uses_selected_build_dockerfile(tmp_path):
    directory = environment(
        tmp_path,
        "services:\n  main:\n    build:\n"
        "      context: ./build\n      dockerfile: Selected\n",
    )
    (directory / "Dockerfile").write_text("FROM scratch\nWORKDIR /wrong\n")
    (directory / "build").mkdir()
    (directory / "build/Selected").write_text("FROM scratch\nWORKDIR /selected\n")
    result = probe(directory, tmp_path, actions=[{"command": "pwd"}])
    assert result == [{"code": 0, "stdout": "/selected\n"}]


def test_main_without_dockerfile_uses_root(tmp_path):
    directory = environment(tmp_path)
    result = probe(directory, tmp_path, actions=[{"command": "pwd"}])
    assert result == [{"code": 0, "stdout": "/\n"}]
