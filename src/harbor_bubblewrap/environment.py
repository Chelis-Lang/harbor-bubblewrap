"""Harbor environment backend: bubblewrap sandboxes with Nix-built tool sets.

Select it with
``harbor run --env harbor_bubblewrap.environment:BubblewrapEnvironment``.
A task needs ``environment/default.nix``. Each service gets one long-lived
sandbox, and each command joins it. So files, processes and the network
persist between commands, as in a container. A task with
``environment/docker-compose.yaml`` runs the Compose subset of ``compose.py``,
and ``default.nix`` then gives one attribute for each service.
See the package README for the supported contract and limits.
"""

from __future__ import annotations

import asyncio
import platform
import shutil
import tempfile
from pathlib import Path, PurePosixPath
from typing import override

from harbor.environments.base import BaseEnvironment, ExecResult
from harbor.environments.capabilities import (
    EnvironmentCapabilities,
    EnvironmentResourceCapabilities,
)

from . import compose, nix
from .sandbox import (
    PATH,
    REQUIRED_TOOLS,
    TRANSFER_DIR,
    Bind,
    Sandbox,
    prepare_root,
    remove_root,
)
from .services import Plan, Runtime

DEFINITION = "default.nix"
COMPOSE = "docker-compose.yaml"
MAIN = "main"
PROGRAMS = ("bwrap", "nix-build", "nix-store")
SOURCE = TRANSFER_DIR / "source"
TARGET = TRANSFER_DIR / "target"
# The main service of a task with no Compose file, and of a Compose file
# that leaves main out. It runs as root, as a Docker image does by default.
DEFAULT_MAIN = compose.Service(
    name=MAIN,
    command=None,
    uid=0,
    gid=0,
    environment={},
    working_dir=None,
    mounts=(),
    depends_on={},
    healthcheck=None,
    network=True,
)


class BubblewrapEnvironment(BaseEnvironment):
    """One Harbor task environment in bubblewrap, built from a Nix definition."""

    @classmethod
    @override
    def preflight(cls) -> None:
        if platform.system() != "Linux":
            raise SystemExit("The bubblewrap environment needs Linux.")
        missing = [name for name in PROGRAMS if shutil.which(name) is None]
        if missing:
            raise SystemExit(
                "The bubblewrap environment needs these programs on PATH: "
                + ", ".join(missing)
            )

    def __init__(self, environment_dir: Path, *args, **kwargs):
        # BaseEnvironment.__init__ reads capabilities and validates the
        # definition, so set this state first.
        self._compose_file = Path(environment_dir) / COMPOSE
        self._services: dict[str, compose.Service] = {}
        self._state: Path | None = None
        self._runtime: Runtime | None = None
        super().__init__(environment_dir, *args, **kwargs)

    @staticmethod
    @override
    def type() -> str:
        return "bubblewrap"

    @classmethod
    @override
    def resource_capabilities(cls) -> EnvironmentResourceCapabilities:
        return EnvironmentResourceCapabilities()

    @property
    @override
    def capabilities(self) -> EnvironmentCapabilities:
        return EnvironmentCapabilities(
            mounted=True,
            disable_internet=True,
            docker_compose=self._compose_file.is_file(),
        )

    @override
    def _validate_definition(self) -> None:
        definition = self.environment_dir / DEFINITION
        if not definition.is_file():
            raise FileNotFoundError(
                f"{definition} not found: the bubblewrap environment builds the "
                f"sandbox from environment/{DEFINITION}"
            )
        services: dict[str, compose.Service] = {}
        if self._compose_file.is_file():
            if self.extra_docker_compose_paths:
                raise ValueError(
                    "The bubblewrap environment does not support Compose overlays"
                )
            services = compose.load(self._compose_file)
        self._services = {MAIN: DEFAULT_MAIN, **services}

    @override
    async def start(self, force_build: bool) -> None:
        # Nix reuses a build when its inputs do not change, so force_build has
        # no effect here.
        self._state = Path(tempfile.mkdtemp(prefix="harbor-bubblewrap-"))
        plans = [await self._plan(name) for name in compose.start_order(self._services)]
        self._runtime = Runtime(plans)
        try:
            await self._runtime.start()
        except BaseException:
            await self._runtime.stop()
            raise
        await self.ensure_dirs(self._mount_targets(writable_only=True))
        await self._upload_environment_dir_after_start()

    async def _plan(self, name: str) -> Plan:
        service = self._services[name]
        state = self._require_state() / name
        state.mkdir()
        definition = self.environment_dir / DEFINITION
        attribute = name if self._compose_file.is_file() else None
        environment = await nix.build(definition, state / "environment", attribute)
        missing = [
            tool for tool in REQUIRED_TOOLS if not (environment / "bin" / tool).exists()
        ]
        if missing:
            raise RuntimeError(
                f"{definition} gives service {name} no {', '.join(missing)} in bin. "
                "The bubblewrap environment needs bash, coreutils and findutils."
            )
        main = name == MAIN
        task_workdir = self.task_env_config.workdir if main else None
        workdir = task_workdir or service.working_dir
        if not workdir:
            dockerfile = service.dockerfile
            if dockerfile is None and main:
                dockerfile = self.environment_dir / "Dockerfile"
            workdir = _dockerfile_workdir(dockerfile) or "/"
        share_network = service.network and self._network_is_public
        root = state / "root"
        prepare_root(
            root,
            hostname=name,
            workdirs=[workdir],
            resolv_conf=Path("/etc/resolv.conf") if share_network else None,
            rootfs=environment / "rootfs",
        )
        sandbox = Sandbox(
            root=root,
            environment=environment,
            closure=await nix.closure(environment),
            binds=self._service_binds(service) + (self._mount_binds() if main else ()),
            share_network=share_network,
            uid=service.uid,
            gid=service.gid,
            hostname=name,
        )
        process_environment = {
            "PATH": PATH,
            "HOME": "/root" if service.uid == 0 else "/",
            **service.environment,
            **(self._startup_env() if main else {}),
        }
        return Plan(
            name=name,
            sandbox=sandbox,
            command=service.command or ("sleep", "infinity"),
            workdir=workdir,
            environment=process_environment,
            healthcheck=service.healthcheck,
        )

    @override
    async def stop(self, delete: bool) -> None:
        runtime, self._runtime = self._runtime, None
        state, self._state = self._state, None
        if runtime is not None:
            await runtime.stop()
        if state is None:
            return
        if delete:
            await asyncio.to_thread(remove_root, state)
        else:
            self.logger.info(f"Kept the bubblewrap sandbox state at {state}")

    @override
    async def stop_service(self, service: str) -> None:
        await self._require_runtime().stop_service(self._service_name(service))

    @override
    async def exec(
        self,
        command: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: int | None = None,
        user: str | int | None = None,
    ) -> ExecResult:
        return await self._exec(
            MAIN,
            command,
            cwd,
            self._merge_env(env),
            timeout_sec,
            self._resolve_user(user),
        )

    @override
    async def service_exec(
        self,
        command: str,
        *,
        service: str | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: int | None = None,
        user: str | int | None = None,
    ) -> ExecResult:
        if self.is_main_service(service):
            return await self.exec(
                command, cwd=cwd, env=env, timeout_sec=timeout_sec, user=user
            )
        return await self._exec(
            self._service_name(service), command, cwd, env, timeout_sec, user
        )

    async def _exec(
        self,
        name: str,
        command: str,
        cwd: str | None,
        env: dict[str, str] | None,
        timeout_sec: int | None,
        user: str | int | None,
    ) -> ExecResult:
        runtime = self._require_runtime()
        plan = runtime.plans[name]
        requested = _requested_uid(user)
        if requested is not None and requested != plan.sandbox.uid:
            raise ValueError(
                f"Service {name} maps only user ID {plan.sandbox.uid}, "
                f"not requested user ID {requested}"
            )
        result = await runtime.exec(
            name,
            ["/bin/bash", "-c", command],
            cwd=cwd or plan.workdir,
            environment={**plan.environment, **(env or {})},
            timeout_sec=timeout_sec,
        )
        return ExecResult(
            stdout=result.stdout, stderr=result.stderr, return_code=result.return_code
        )

    @override
    async def upload_file(self, source_path: Path | str, target_path: str) -> None:
        source = Path(source_path).resolve(strict=True)
        await self._require_runtime().copy(
            MAIN,
            'mkdir -p -- "$(dirname -- "$1")" && cp -- "$2" "$1"',
            [target_path, str(SOURCE)],
            Bind(source, SOURCE, writable=False),
        )

    @override
    async def upload_dir(self, source_dir: Path | str, target_dir: str) -> None:
        source = Path(source_dir).resolve(strict=True)
        await self._require_runtime().copy(
            MAIN,
            'mkdir -p -- "$1" && cp -R -- "$2"/. "$1"',
            [target_dir, str(SOURCE)],
            Bind(source, SOURCE, writable=False),
        )

    @override
    async def download_file(self, source_path: str, target_path: Path | str) -> None:
        await self._download_file(MAIN, source_path, target_path)

    @override
    async def download_dir(self, source_dir: str, target_dir: Path | str) -> None:
        await self._download_dir(MAIN, source_dir, target_dir)

    @override
    async def service_download_file(
        self, source_path: str, target_path: Path | str, *, service: str | None = None
    ) -> None:
        await self._download_file(self._service_name(service), source_path, target_path)

    @override
    async def service_download_dir(
        self, source_dir: str, target_dir: Path | str, *, service: str | None = None
    ) -> None:
        await self._download_dir(self._service_name(service), source_dir, target_dir)

    async def _download_file(
        self, name: str, source_path: str, target_path: Path | str
    ) -> None:
        target = Path(target_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(dir=self._require_state()))
        try:
            await self._require_runtime().copy(
                name,
                'cp -- "$1" "$2"/file',
                [source_path, str(TARGET)],
                Bind(staging, TARGET, writable=True),
            )
            shutil.move(staging / "file", target)
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    async def _download_dir(
        self, name: str, source_dir: str, target_dir: Path | str
    ) -> None:
        target = Path(target_dir)
        target.mkdir(parents=True, exist_ok=True)
        await self._require_runtime().copy(
            name,
            'cp -R -- "$1"/. "$2"',
            [source_dir, str(TARGET)],
            Bind(target.resolve(), TARGET, writable=True),
        )

    def _service_binds(self, service: compose.Service) -> tuple[Bind, ...]:
        """Named volumes are host directories in the sandbox state, shared by services."""
        binds = []
        for mount in service.mounts:
            if mount.volume:
                source = self._require_state() / "volumes" / mount.source
            else:
                source = Path(mount.source)
            if not source.exists():
                source.mkdir(parents=True, exist_ok=True)
            binds.append(
                Bind(source, PurePosixPath(mount.target), writable=not mount.read_only)
            )
        return tuple(binds)

    def _mount_binds(self) -> tuple[Bind, ...]:
        """The host directories that Harbor binds into main, such as /logs."""
        binds = []
        for mount in self._mounts:
            if mount["type"] != "bind":
                raise ValueError(
                    "The bubblewrap environment supports only bind mounts, not "
                    f"{mount['type']!r} at {mount['target']}"
                )
            source = Path(mount["source"])
            if not source.exists():
                if mount.get("bind", {}).get("create_host_path") is False:
                    raise FileNotFoundError(f"bind mount source not found: {source}")
                source.mkdir(parents=True)
            binds.append(
                Bind(
                    source.resolve(),
                    PurePosixPath(mount["target"]),
                    writable=not mount.get("read_only", False),
                )
            )
        return tuple(binds)

    def _service_name(self, service: str | None) -> str:
        name = MAIN if self.is_main_service(service) else str(service)
        if name not in self._services:
            raise ValueError(f"The task defines no service {name!r}")
        return name

    def _require_runtime(self) -> Runtime:
        if self._runtime is None:
            raise RuntimeError("The bubblewrap environment is not started")
        return self._runtime

    def _require_state(self) -> Path:
        if self._state is None:
            raise RuntimeError("The bubblewrap environment is not started")
        return self._state


def _requested_uid(user: str | int | None) -> int | None:
    """Return a numeric user ID. Root is the mapped service user alias."""
    if user is None:
        return None
    if user == "root":
        return None
    if isinstance(user, int):
        return user
    if user.isdigit():
        return int(user)
    raise ValueError(
        "The bubblewrap environment runs commands as root or as a numeric user "
        f"ID, not as {user!r}"
    )


def _dockerfile_workdir(path: Path | None) -> str | None:
    """Read the final-stage WORKDIR from any service Dockerfile name."""
    if path is None or not path.exists():
        return None
    from dockerfile_parse import DockerfileParser

    # The path API appends /Dockerfile to names such as world.Dockerfile.
    with path.open("rb") as source:
        instructions = DockerfileParser(fileobj=source).structure
    workdir = None
    for instruction in instructions:
        name = instruction["instruction"]
        if name == "FROM":
            workdir = None
        elif name == "WORKDIR":
            value = instruction["value"].strip()
            if value:
                workdir = str(PurePosixPath(workdir or "/") / value)
    return workdir
