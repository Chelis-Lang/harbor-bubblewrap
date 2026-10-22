"""The Compose subset of the bubblewrap backend: what it runs and what it refuses."""

from __future__ import annotations

from pathlib import Path

import pytest
from harbor_bubblewrap import compose


@pytest.mark.parametrize(
    ("service", "volumes", "message"),
    [
        ({"ports": ["80:80"]}, None, "unsupported keys"),
        ({"network_mode": "host"}, None, "network_mode"),
        ({"user": "agent"}, None, "must be numeric"),
        ({"volumes": ["data:/data"]}, None, "not declared"),
        ({"volumes": ["data:/data"]}, {"data": {"external": True}}, "external"),
        (
            {"depends_on": {"main": {"condition": "service_completed_successfully"}}},
            None,
            "depends_on",
        ),
    ],
)
def test_unsupported_compose_meaning_is_refused(service, volumes, message):
    document = {"services": {"main": {}, "world": service}}
    if volumes is not None:
        document["volumes"] = volumes
    with pytest.raises(compose.ComposeError, match=message):
        compose.parse(document, Path("/project"))


def test_dependency_cycle_is_refused():
    document = {"services": {"a": {"depends_on": ["b"]}, "b": {"depends_on": ["a"]}}}
    with pytest.raises(compose.ComposeError, match="cycle"):
        compose.parse(document, Path("/project"))


@pytest.mark.parametrize(
    ("value", "seconds"), [("1m30s", 90.0), ("500ms", 0.5), ("2h", 7200.0)]
)
def test_durations_use_compose_units(value, seconds):
    assert compose.duration(value) == seconds


@pytest.mark.parametrize("value", ["5", "1x", "s", 3, 0.5, True, None])
def test_durations_without_units_are_refused(value):
    with pytest.raises(compose.ComposeError):
        compose.duration(value)


@pytest.mark.parametrize(
    "field",
    [
        "cap_add",
        "cap_drop",
        "cpus",
        "init",
        "logging",
        "mem_limit",
        "mem_reservation",
        "pids_limit",
        "restart",
        "security_opt",
        "stdin_open",
        "stop_grace_period",
        "stop_signal",
        "tty",
        "ulimits",
        "platform",
    ],
)
def test_runtime_options_are_refused(field):
    with pytest.raises(compose.ComposeError):
        compose.parse({"services": {"main": {field: None}}}, Path("/project"))


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (
            {
                "TEXT": "a=b",
                "EMPTY": "",
                "INT": 42,
                "FLOAT": 1.5,
                "TRUE": True,
                "FALSE": False,
            },
            {
                "TEXT": "a=b",
                "EMPTY": "",
                "INT": "42",
                "FLOAT": "1.5",
                "TRUE": "true",
                "FALSE": "false",
            },
        ),
        (["TEXT=a=b", "EMPTY="], {"TEXT": "a=b", "EMPTY": ""}),
    ],
)
def test_explicit_environment_values(value, expected):
    services = compose.parse(
        {"services": {"main": {"environment": value}}}, Path("/project")
    )
    assert services["main"].environment == expected


@pytest.mark.parametrize(
    "value",
    [
        {"KEY": None},
        {"KEY": []},
        {"KEY": {}},
        {1: "value"},
        ["KEY"],
        [None],
        [42],
        "KEY=value",
        False,
        42,
    ],
)
def test_environment_without_explicit_scalar_values_is_refused(value, monkeypatch):
    monkeypatch.setenv("KEY", "host-secret")
    with pytest.raises(compose.ComposeError):
        compose.parse({"services": {"main": {"environment": value}}}, Path("/project"))


@pytest.mark.parametrize("healthcheck", [None, {"disable": True}, {"test": ["NONE"]}])
def test_healthy_dependency_requires_healthcheck(healthcheck):
    document = {
        "services": {
            "main": {"depends_on": {"world": {"condition": "service_healthy"}}},
            "world": {"healthcheck": healthcheck},
        }
    }
    with pytest.raises(compose.ComposeError):
        compose.parse(document, Path("/project"))


@pytest.mark.parametrize("required", [False, None, 0, "true"])
def test_optional_or_invalid_dependency_is_refused(required):
    document = {
        "services": {
            "main": {"depends_on": {"world": {"required": required}}},
            "world": {},
        }
    }
    with pytest.raises(compose.ComposeError):
        compose.parse(document, Path("/project"))


def test_explicit_required_dependency_preserves_start_order():
    services = compose.parse(
        {
            "services": {
                "main": {
                    "depends_on": {
                        "world": {
                            "condition": "service_healthy",
                            "required": True,
                        }
                    }
                },
                "world": {"healthcheck": {"test": ["CMD", "true"]}},
            }
        },
        Path("/project"),
    )
    assert compose.start_order(services) == ["world", "main"]


@pytest.mark.parametrize("field", ["interval", "timeout", "start_period"])
@pytest.mark.parametrize("value", [1, 0.5, True])
def test_numeric_healthcheck_duration_is_refused(field, value):
    with pytest.raises(compose.ComposeError):
        compose.parse(
            {
                "services": {
                    "main": {
                        "healthcheck": {"test": ["CMD", "true"], field: value},
                    }
                }
            },
            Path("/project"),
        )


@pytest.mark.parametrize(
    ("build", "expected"),
    [
        (None, None),
        ("service", "/project/service/Dockerfile"),
        (
            {"context": "service", "dockerfile": "Dockerfile.worker"},
            "/project/service/Dockerfile.worker",
        ),
        ({"dockerfile": "Dockerfile.worker"}, "/project/Dockerfile.worker"),
    ],
)
def test_build_metadata_selects_service_dockerfile(build, expected):
    service = {} if build is None else {"build": build}
    services = compose.parse({"services": {"main": service}}, Path("/project"))
    assert services["main"].dockerfile == (Path(expected) if expected else None)
