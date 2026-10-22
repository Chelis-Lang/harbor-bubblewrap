"""The Docker Compose subset that the bubblewrap backend runs.

The backend reads ``environment/docker-compose.yaml`` and maps each service to
one long-lived sandbox. It accepts the keys that dev/hell task layouts use. It
refuses each other key, so a task never runs with a silently changed meaning.
The Nix definition replaces the image of each service.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

# Nix replaces image selection and builds. The other keys contain metadata.
IGNORED = frozenset(
    {
        "build",
        "container_name",
        "image",
        "labels",
        "pull_policy",
    }
)
SUPPORTED = frozenset(
    {
        "command",
        "depends_on",
        "entrypoint",
        "environment",
        "healthcheck",
        "network_mode",
        "user",
        "volumes",
        "working_dir",
    }
)
TOP_LEVEL = frozenset({"name", "services", "version", "volumes"})
CONDITIONS = frozenset({"service_started", "service_healthy"})
DURATION = re.compile(r"(\d+(?:\.\d+)?)(ns|us|ms|s|m|h)")
UNITS = {"ns": 1e-9, "us": 1e-6, "ms": 1e-3, "s": 1.0, "m": 60.0, "h": 3600.0}


class ComposeError(ValueError):
    """The Compose file uses a key or a value outside the supported subset."""


@dataclass(frozen=True)
class Mount:
    """A named volume or a host directory that a service sees at ``target``."""

    source: str
    target: str
    read_only: bool
    volume: bool


@dataclass(frozen=True)
class Healthcheck:
    argv: tuple[str, ...]
    interval: float
    timeout: float
    retries: int
    start_period: float


@dataclass(frozen=True)
class Service:
    name: str
    command: tuple[str, ...] | None
    uid: int
    gid: int
    environment: Mapping[str, str]
    working_dir: str | None
    mounts: tuple[Mount, ...]
    depends_on: Mapping[str, str]
    healthcheck: Healthcheck | None
    network: bool
    dockerfile: Path | None = None


def load(path: Path) -> dict[str, Service]:
    """Read a Compose file and return its services by name."""
    import yaml  # Harbor installs PyYAML. The core environment does not need it.

    return parse(yaml.safe_load(path.read_text()) or {}, path.parent)


def parse(document: Mapping, project_dir: Path) -> dict[str, Service]:
    """Return the services of a parsed Compose document by name."""
    unknown = set(document) - TOP_LEVEL - {k for k in document if k.startswith("x-")}
    if unknown:
        raise ComposeError(f"unsupported top-level Compose keys: {sorted(unknown)}")
    volumes = _volumes(document.get("volumes") or {})
    services = {
        name: _service(name, spec or {}, volumes, project_dir)
        for name, spec in (document.get("services") or {}).items()
    }
    for service in services.values():
        for dependency in service.depends_on:
            if dependency not in services:
                raise ComposeError(f"{service.name} depends on unknown {dependency}")
            if (
                service.depends_on[dependency] == "service_healthy"
                and services[dependency].healthcheck is None
            ):
                raise ComposeError(
                    f"{service.name} requires a healthcheck on {dependency}"
                )
    start_order(services)
    return services


def start_order(services: Mapping[str, Service]) -> list[str]:
    """Return service names so that each service follows its dependencies."""
    order: list[str] = []
    visiting: set[str] = set()

    def visit(name: str) -> None:
        if name in order:
            return
        if name in visiting:
            raise ComposeError(f"dependency cycle at service {name}")
        visiting.add(name)
        for dependency in sorted(services[name].depends_on):
            visit(dependency)
        visiting.remove(name)
        order.append(name)

    for name in sorted(services):
        visit(name)
    return order


def _volumes(declared: Mapping) -> set[str]:
    for name, spec in declared.items():
        spec = spec or {}
        if spec.get("external"):
            raise ComposeError(f"external volume {name} is not supported")
        extra = set(spec) - {"name", "labels"}
        if extra:
            raise ComposeError(f"volume {name} uses unsupported keys {sorted(extra)}")
    return set(declared)


def _service(name: str, spec: Mapping, volumes: set[str], project_dir: Path) -> Service:
    unknown = set(spec) - SUPPORTED - IGNORED
    if unknown:
        raise ComposeError(f"service {name} uses unsupported keys {sorted(unknown)}")
    network = spec.get("network_mode")
    if network not in (None, "none"):
        raise ComposeError(f"service {name}: network_mode {network!r} is not supported")
    uid, gid = _user(name, spec.get("user"))
    working_dir = spec.get("working_dir")
    if working_dir is not None and not PurePosixPath(working_dir).is_absolute():
        raise ComposeError(f"service {name}: working_dir must be absolute")
    return Service(
        name=name,
        command=_command(spec),
        uid=uid,
        gid=gid,
        environment=_environment(name, spec.get("environment", {})),
        working_dir=working_dir,
        mounts=tuple(
            _mount(name, entry, volumes, project_dir)
            for entry in spec.get("volumes") or []
        ),
        depends_on=_depends_on(name, spec.get("depends_on") or {}),
        healthcheck=_healthcheck(name, spec.get("healthcheck")),
        network=network is None,
        dockerfile=_dockerfile(spec.get("build"), project_dir),
    )


def _dockerfile(build, project_dir: Path) -> Path | None:
    if build is None:
        return None
    if isinstance(build, str):
        context, filename = build, "Dockerfile"
    else:
        context = build.get("context", ".")
        filename = build.get("dockerfile", "Dockerfile")
    return (project_dir / context / filename).resolve()


def _argv(value) -> tuple[str, ...]:
    if isinstance(value, str):
        return tuple(shlex.split(value))
    return tuple(str(part) for part in value)


def _command(spec: Mapping) -> tuple[str, ...] | None:
    entrypoint = spec.get("entrypoint")
    command = spec.get("command")
    if entrypoint is None and command is None:
        return None
    return _argv(entrypoint or ()) + _argv(command or ())


def _user(name: str, value) -> tuple[int, int]:
    """Return user and group IDs. Docker uses group 0 when the group is absent."""
    if value is None:
        return 0, 0
    user, _, group = str(value).partition(":")
    if not user.isdigit() or (group and not group.isdigit()):
        raise ComposeError(f"service {name}: user {value!r} must be numeric")
    return int(user), int(group or 0)


def _environment(name: str, value) -> dict[str, str]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        result = {}
        for key, item in value.items():
            if not isinstance(key, str) or not isinstance(item, (str, int, float)):
                raise ComposeError(
                    f"service {name}: environment {key!r} needs an explicit scalar value"
                )
            result[key] = str(item).lower() if isinstance(item, bool) else str(item)
        return result
    if not isinstance(value, list):
        raise ComposeError(f"service {name}: environment must be a mapping or list")
    result = {}
    for entry in value:
        if not isinstance(entry, str):
            raise ComposeError(f"service {name}: environment needs string entries")
        key, separator, item = entry.partition("=")
        if not separator:
            raise ComposeError(f"service {name}: environment {entry!r} needs a value")
        result[key] = item
    return result


def _mount(name: str, entry, volumes: set[str], project_dir: Path) -> Mount:
    if isinstance(entry, str):
        parts = entry.split(":")
        if len(parts) not in (2, 3) or (
            len(parts) == 3 and parts[2] not in ("ro", "rw")
        ):
            raise ComposeError(f"service {name}: volume {entry!r} is not supported")
        source, target, read_only = parts[0], parts[1], parts[2:] == ["ro"]
        kind = "volume" if not source.startswith(("/", ".", "~")) else "bind"
    else:
        extra = set(entry) - {"type", "source", "target", "read_only"}
        if extra:
            raise ComposeError(
                f"service {name}: volume keys {sorted(extra)} are not supported"
            )
        kind, source, target = (
            entry.get("type"),
            entry.get("source"),
            entry.get("target"),
        )
        read_only = bool(entry.get("read_only", False))
        if kind not in ("volume", "bind") or not source or not target:
            raise ComposeError(f"service {name}: volume {entry!r} is not supported")
    if not PurePosixPath(target).is_absolute():
        raise ComposeError(f"service {name}: volume target {target!r} must be absolute")
    if kind == "volume":
        if source not in volumes:
            raise ComposeError(f"service {name}: volume {source!r} is not declared")
        return Mount(source, target, read_only, volume=True)
    host = (project_dir / Path(source).expanduser()).resolve()
    return Mount(str(host), target, read_only, volume=False)


def _depends_on(name: str, value) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {str(dependency): "service_started" for dependency in value}
    result = {}
    for dependency, spec in value.items():
        spec = spec or {}
        extra = set(spec) - {"condition", "required"}
        condition = spec.get("condition", "service_started")
        if (
            extra
            or condition not in CONDITIONS
            or spec.get("required", True) is not True
        ):
            raise ComposeError(
                f"service {name}: depends_on {dependency} is not supported"
            )
        result[str(dependency)] = condition
    return result


def _healthcheck(name: str, value) -> Healthcheck | None:
    if value is None or value.get("disable"):
        return None
    extra = set(value) - {"test", "interval", "timeout", "retries", "start_period"}
    if extra:
        raise ComposeError(
            f"service {name}: healthcheck keys {sorted(extra)} are not supported"
        )
    test = value.get("test")
    if isinstance(test, str):
        argv: tuple[str, ...] = ("/bin/sh", "-c", test)
    elif test and test[0] == "CMD":
        argv = tuple(str(part) for part in test[1:])
    elif test and test[0] == "CMD-SHELL":
        argv = ("/bin/sh", "-c", str(test[1]))
    elif test and test[0] == "NONE":
        return None
    else:
        raise ComposeError(
            f"service {name}: healthcheck test {test!r} is not supported"
        )
    return Healthcheck(
        argv=argv,
        interval=duration(value.get("interval", "30s")),
        timeout=duration(value.get("timeout", "30s")),
        retries=int(value.get("retries", 3)),
        start_period=duration(value.get("start_period", "0s")),
    )


def duration(value) -> float:
    """Return seconds for a Compose duration such as ``1m30s`` or ``500ms``."""
    if not isinstance(value, str):
        raise ComposeError(f"duration {value!r} is not supported")
    text = value.strip()
    parts = DURATION.findall(text)
    if not parts or "".join(number + unit for number, unit in parts) != text:
        raise ComposeError(f"duration {value!r} is not supported")
    return sum(float(number) * UNITS[unit] for number, unit in parts)
