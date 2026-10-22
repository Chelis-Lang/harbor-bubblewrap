"""Harbor example tasks in the bubblewrap backend.

These tests need Linux namespaces, bubblewrap, Nix, and an installed Harbor extra.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from importlib.resources import files
from pathlib import Path

import pytest

TASKS = Path(str(files("harbor_bubblewrap").joinpath("tasks")))
PROBE_ENVIRONMENT = Path(str(files("harbor_bubblewrap").joinpath("probe")))
BACKEND = "harbor_bubblewrap.environment:BubblewrapEnvironment"

pytestmark = pytest.mark.skipif(
    os.environ.get("HELL_TEST_NAMESPACES") != "1",
    reason="the Linux namespace tier runs the bubblewrap backend",
)

# Start one environment, run each command in it in order, and print the results.
PROBE = """
import asyncio
import json
import sys
from pathlib import Path

from harbor.models.task.config import EnvironmentConfig
from harbor.models.trial.paths import TrialPaths

from harbor_bubblewrap.environment import BubblewrapEnvironment


async def main(environment_dir, trial_dir, commands):
    environment = BubblewrapEnvironment(
        environment_dir=Path(environment_dir),
        environment_name="probe",
        session_id="probe__env",
        trial_paths=TrialPaths(Path(trial_dir)),
        task_env_config=EnvironmentConfig(workdir="/app"),
    )
    await environment.start(force_build=False)
    results = []
    try:
        for command in json.loads(commands):
            result = await environment.exec(command, timeout_sec=60)
            results.append(
                {"code": result.return_code, "stdout": result.stdout or ""}
            )
    finally:
        await environment.stop(delete=True)
    print(json.dumps(results))


asyncio.run(main(*sys.argv[1:]))
"""


def harbor_python() -> Path:
    return Path(sys.executable)


def run_harbor(task: str, agent: str, jobs: Path) -> dict:
    command = [
        str(harbor_python().parent / "harbor"),
        "run",
        "--path",
        str(TASKS / task),
        "--agent",
        agent,
        "--env",
        BACKEND,
        "--jobs-dir",
        str(jobs),
        "--job-name",
        "job",
    ]
    completed = subprocess.run(
        command, capture_output=True, text=True, timeout=900, check=False
    )
    output = completed.stdout + completed.stderr
    assert completed.returncode == 0, output
    results = sorted((jobs / "job").glob("*/result.json"))
    assert len(results) == 1, output
    result = json.loads(results[0].read_text())
    assert result["exception_info"] is None, result["exception_info"]
    assert result["config"]["environment"]["import_path"] == BACKEND
    return result


def probe(environment_dir: Path, trial_dir: Path, commands: list[str]) -> list[dict]:
    completed = subprocess.run(
        [
            str(harbor_python()),
            "-c",
            PROBE,
            str(environment_dir),
            str(trial_dir),
            json.dumps(commands),
        ],
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return json.loads(completed.stdout.splitlines()[-1])


@pytest.mark.parametrize(
    "task", ["hello-file", "python-fibonacci", "git-commit", "world-challenge"]
)
def test_oracle_gets_full_reward_in_bubblewrap(task, tmp_path):
    result = run_harbor(task, "oracle", tmp_path)
    assert result["verifier_result"]["rewards"] == {"reward": 1.0}


@pytest.mark.parametrize("task", ["hello-file", "world-challenge"])
def test_nop_agent_gets_no_reward_in_bubblewrap(task, tmp_path):
    result = run_harbor(task, "nop", tmp_path)
    assert result["verifier_result"]["rewards"] == {"reward": 0.0}


def test_sandbox_sees_only_the_nix_closure(tmp_path):
    environment_dir = TASKS / "hello-file/environment"
    built = subprocess.run(
        ["nix-build", str(environment_dir / "default.nix"), "--no-out-link"],
        capture_output=True,
        text=True,
        check=True,
    )
    closure = subprocess.run(
        ["nix-store", "--query", "--requisites", built.stdout.split()[-1]],
        capture_output=True,
        text=True,
        check=True,
    )
    tools, store, host = probe(
        environment_dir,
        tmp_path,
        [
            (
                "for tool in bash find python3 git apt-get; do "
                "command -v $tool > /dev/null && echo $tool; done; true"
            ),
            "ls /nix/store",
            "ls -d /work /home /etc/os-release 2>/dev/null; true",
        ],
    )
    assert tools["stdout"].split() == ["bash", "find"]
    assert sorted(store["stdout"].split()) == sorted(
        Path(path).name for path in closure.stdout.split()
    )
    assert host["stdout"].split() == []


def test_commands_share_processes_files_and_network(tmp_path):
    # The first command leaves a process that holds its output open. The command
    # must still return when the shell exits, as docker exec does.
    start, alive, fetched = probe(
        PROBE_ENVIRONMENT,
        tmp_path,
        [
            (
                "sleep 600 & echo $! > /tmp/sleeper; "
                "python3 -m http.server 8765 --bind 127.0.0.1 --directory /tmp "
                "> /tmp/http.log 2>&1 & echo started"
            ),
            'kill -0 "$(cat /tmp/sleeper)" && echo alive',
            (
                "for attempt in $(seq 50); do python3 -c 'import urllib.request; "
                'print(urllib.request.urlopen("http://127.0.0.1:8765/sleeper").read().decode())\''
                " 2>/dev/null && break; sleep 0.1; done"
            ),
        ],
    )
    assert (start["code"], start["stdout"].strip()) == (0, "started")
    assert (alive["code"], alive["stdout"].strip()) == (0, "alive")
    assert fetched["stdout"].strip().isdigit()


def test_tmux_session_persists_between_commands(tmp_path):
    # Terminus-2 drives a tmux session with one command for each step.
    _, sent, pane = probe(
        PROBE_ENVIRONMENT,
        tmp_path,
        [
            "tmux new-session -d -s agent -x 80 -y 24",
            "tmux send-keys -t agent 'echo marker-$((6 * 7))' Enter",
            (
                "for attempt in $(seq 50); do tmux capture-pane -p -t agent "
                "| grep -q '^marker-42$' && break; sleep 0.1; done; "
                "tmux capture-pane -p -t agent"
            ),
        ],
    )
    assert sent["code"] == 0
    assert "marker-42" in pane["stdout"].split()


BOUNDARY_PROBE = """
import asyncio
import json
import os
import shlex
import socket
import sys
from pathlib import Path

from harbor.models.task.config import EnvironmentConfig, NetworkPolicy
from harbor.models.trial.paths import TrialPaths
from harbor_bubblewrap.environment import BubblewrapEnvironment


async def main(environment_dir, trial_dir):
    trial = Path(trial_dir)
    trial.mkdir()
    secret = trial / "host-only"
    secret.write_text("host-private-fixture")
    source = trial / "input"
    source.write_text("public-payload")
    target = trial / "download"
    forbidden = trial / "forbidden"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    address = chr(0) + "harbor-test-" + str(os.getpid())
    listener.bind(address)
    listener.listen(4)
    control = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    control.connect(address)
    control.close()
    environment = BubblewrapEnvironment(
        environment_dir=Path(environment_dir),
        environment_name="boundary",
        session_id="boundary__env",
        trial_paths=TrialPaths(trial / "trial"),
        task_env_config=EnvironmentConfig(workdir="/app"),
        network_policy=NetworkPolicy(network_mode="no-network"),
    )
    results = {}
    try:
        await environment.start(force_build=False)
        inspect = (
            "import json, os, stat; "
            "print(json.dumps({"
            "'canary': b'HELL_BWRAP_HOST_CANARY=' in open('/proc/1/environ', 'rb').read(), "
            "'regular_files': [name for name in os.listdir('/proc/1/fd') "
            "if stat.S_ISREG(os.stat('/proc/1/fd/' + name).st_mode)]}))"
        )
        result = await environment.exec("python3 -c " + shlex.quote(inspect))
        assert result.return_code == 0, result.stderr
        results["process"] = json.loads(result.stdout)
        connect = (
            "import socket; s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); "
            "s.connect(" + repr(address) + ")"
        )
        result = await environment.exec("python3 -c " + shlex.quote(connect))
        results["network_blocked"] = (
            result.return_code != 0 and "ConnectionRefusedError" in result.stderr
        )
        command = (
            "ln -s /app/input /app/link && ln -s "
            + shlex.quote(str(secret))
            + " /app/host-only"
        )
        result = await environment.exec(command)
        assert result.return_code == 0, result.stderr
        try:
            await environment.download_file("/app/host-only", forbidden)
        except RuntimeError:
            results["host_link_blocked"] = not forbidden.exists()
        else:
            results["host_link_blocked"] = False
        command = (
            "mkdir -p /app/data && printf 'payload' > /app/data/real && "
            "ln -s " + shlex.quote(str(secret)) + " /app/data/leak"
        )
        result = await environment.exec(command)
        assert result.return_code == 0, result.stderr
        directory = trial / "download-dir"
        try:
            await environment.download_dir("/app/data", directory)
        except RuntimeError:
            results["dir_link_blocked"] = list(directory.iterdir()) == []
        else:
            results["dir_link_blocked"] = False
        result = await environment.exec("printf '/etc/absent.so\\n' > /etc/ld.so.preload")
        assert result.return_code == 0, result.stderr
        try:
            await environment.download_file("/app/absent-input", trial / "absent")
        except RuntimeError as error:
            results["preload_ignored"] = "ld.so" not in str(error)
        else:
            results["preload_ignored"] = False
        emit = (
            "with open('/proc/1/fd/1', 'w') as stream: "
            "stream.write('old-prefix' + 'x' * 65536 + 'service-diagnostic')"
        )
        result = await environment.exec("python3 -c " + shlex.quote(emit))
        assert result.return_code == 0, result.stderr
        result = await environment.exec(
            "rm -- /bin && mkdir -- /bin && "
            "printf '#!/usr/bin/bash\\\\nexit 91\\\\n' > /bin/bash && chmod +x /bin/bash"
        )
        assert result.return_code == 0, result.stderr
        await environment.upload_file(source, "/app/input")
        await environment.stop_service("main")
        await environment.download_file("/app/link", target)
        results["download"] = target.read_text()
        try:
            await environment.exec("true")
        except RuntimeError as error:
            results["stopped_error"] = str(error)
    finally:
        await environment.stop(delete=True)
        listener.close()
    print(json.dumps(results))


asyncio.run(main(*sys.argv[1:]))
"""


def test_host_boundary_and_transfers_survive_service_changes(tmp_path, monkeypatch):
    monkeypatch.setenv("HELL_BWRAP_HOST_CANARY", "host-private-fixture")
    completed = subprocess.run(
        [
            str(harbor_python()),
            "-c",
            BOUNDARY_PROBE,
            str(PROBE_ENVIRONMENT),
            str(tmp_path / "boundary"),
        ],
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    result = json.loads(completed.stdout.splitlines()[-1])
    assert result["process"] == {"canary": False, "regular_files": []}
    assert result["network_blocked"] is True
    assert result["host_link_blocked"] is True
    assert result["dir_link_blocked"] is True
    assert result["preload_ignored"] is True
    assert result["download"] == "public-payload"
    assert result["stopped_error"].endswith("service-diagnostic")
    assert "old-prefix" not in result["stopped_error"]
    assert len(result["stopped_error"]) < 4100
