# harbor-bubblewrap

A Harbor environment plug-in with bubblewrap sandboxes and Nix tool sets.
The backend works with stock Harbor 0.24.0 and needs no Docker daemon.
The import path is `harbor_bubblewrap.environment:BubblewrapEnvironment`.

## Install

Use Python 3.12 or later on Linux.
Install `bubblewrap`, Nix, `uidmap`, and a system Python 3.12 for the mapped ownership test.
The host must allow unprivileged user namespaces and subordinate user and group mappings.
Ubuntu hosts can need `kernel.apparmor_restrict_unprivileged_userns=0`.
This changes a host-wide security control.
Use a dedicated Linux worker for that configuration.
Use an init process in containers to reap namespace workers.

Install from a source checkout:

```sh
git clone https://github.com/Chelis-Lang/harbor-bubblewrap.git
cd harbor-bubblewrap
python3.12 -m venv .venv
.venv/bin/python -m pip install '.[harbor]'
```

The Harbor extra pins stock Harbor 0.24.0 and dockerfile-parse 2.0.1.
The core package needs PyYAML, but it does not import Harbor through `__init__.py`.
Low-level modules do not need the Harbor extra.
The [contributor guide](CONTRIBUTING.md) gives the local distribution check command.

`tools/install-nix.sh` installs pinned single-user Nix 2.34.4 and checks its published SHA256.
Create `/nix` with ownership for the current user before this script runs.
Then source `$HOME/.nix-profile/etc/profile.d/nix.sh`.
The script needs `curl`, `tar`, `xz`, and `sha256sum`.

## Usage

The wheel contains four complete example tasks: `hello-file`, `python-fibonacci`, `git-commit`, and `world-challenge`.
Run all four with stock Harbor:

```sh
TASKS="$(.venv/bin/python - <<'PY'
from importlib.resources import files
print(files("harbor_bubblewrap").joinpath("tasks"))
PY
)"
.venv/bin/harbor run --path "$TASKS" --agent oracle \
  --env harbor_bubblewrap.environment:BubblewrapEnvironment \
  --jobs-dir ./jobs
```

Use `--path "$TASKS/hello-file"` for one task.
Use `--agent nop` for a negative control.
The example definitions retain their original Nix hashes and Docker image pins.
No Docker daemon is necessary for the bubblewrap backend.

## Supported contract

Each task needs `environment/default.nix`.
Without Compose, this file must evaluate to a derivation with bash, coreutils, and findutils in `bin`.
Add all tools that the task, solution, tests, and agent need.
For example, Terminus-2 needs `tmux`, and artifact exclusions need `tar` and `gzip`.
The result can contain a `rootfs` directory for files at fixed paths.
The backend copies that tree into the writable sandbox root and follows its links.

With `environment/docker-compose.yaml`, the Nix file must return one derivation for each service name.
An omitted `main` service uses root and `sleep infinity`.
The parser supports these service fields:

- `command`, `entrypoint`, numeric `user`, explicit `environment`, and `working_dir`.
- Bind mounts and declared, non-external named `volumes`.
- Mandatory `depends_on` with `service_started` or `service_healthy`.
- `healthcheck` with Docker duration units, retries, and start periods.
- `network_mode: none`.

Nix replaces `image` and `build`.
The selected build Dockerfile supplies WORKDIR metadata, not a container image.
The parser accepts `container_name`, `labels`, and `pull_policy` as metadata.
It refuses unsupported fields instead of silently discarding their meaning.

For `main`, the task workdir overrides Compose, then the selected Dockerfile WORKDIR, then `/`.
Other services use the same order without the task workdir.
Each service retains files, background processes, and network state between commands.
Services start in dependency order and honor healthchecks.
Uploads and downloads use immutable Nix tools and also work after service stop.
Links resolve inside the sandbox, not on the host.
A download lands in a host directory only when each entry is a regular file or a directory.
The backend refuses a link or a special file, so it never hands the host a path from the sandbox.
A copy process also loads no library named by the writable root.
A copy leaves the root as it found it.

Holder failures retain a bounded 4,000-byte diagnostic tail.
Command and healthcheck output has no package byte limit.
A separate verifier needs `tests/default.nix` and either `tests/Dockerfile` or `tests/docker-compose.yaml`.
Harbor does not select a separate verifier from the Nix definition alone.
For bundled tests, place test files under `rootfs/tests`.

## Limits and trust

- Use trusted task definitions only. Nix evaluation runs as the host user, outside the sandbox. Compose bind sources and `rootfs` inputs also need trust.
- Public task networking shares the host network. It exposes host loopback services and abstract Unix sockets. Do not use it for untrusted networked tasks.
- Service names do not resolve. There are no Docker bridge networks, ports, extra networks, external volumes, or Compose overlays.
- The general environment maps one user ID per service. Other IDs and user names fail. Harbor housekeeping with `user="root"` uses the mapped service user.
- CPU and memory enforcement modes fail. The backend sets no CPU or memory limits.
- `force_build` has no effect because Nix reuses unchanged inputs.
- The service command controls the sandbox lifetime. Its exit ends the sandbox.
- There is no `/lib` loader for arbitrary downloaded Linux binaries. Use programs from the Nix closure.
- This package supplies sandbox execution, not application-specific access policy, output validation, or evidence checks.

## Tests and CI

Pure Python tests cover Compose validation and host process retirement.
Linux namespace tests cover all four oracle tasks, negative controls, isolation, persistent commands, transfers, workdirs, users, and mapped ownership.
These checks prove execution behavior, not model capability or a security certification.
The probe definition is package data at `harbor_bubblewrap/probe/default.nix`.

```sh
.venv/bin/python -m pytest -q
HELL_TEST_NAMESPACES=1 .venv/bin/python -m pytest -q
```

The second command needs the Linux host tools and namespace permissions above.
Tests use their current Python interpreter for Harbor.
CI builds the source distribution and wheel, then tests the installed wheel outside the source checkout.
A core job omits Harbor. A Linux job installs the Harbor extra and enables namespace tests.
CI uses the unchanged pinned Nix installer and needs no private repository credentials.

## Project guides

- [Contribute and check a distribution](CONTRIBUTING.md).
- [Report a security issue and review the trust boundary](SECURITY.md).
- [Read the change history](CHANGELOG.md).

## License

MIT. Copyright (c) 2026 2nd Set AI Corp.
See [LICENSE](LICENSE) for the complete terms.
