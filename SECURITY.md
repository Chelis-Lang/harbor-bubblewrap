# Security policy

## Report a vulnerability

Do not place exploit details, credentials, or private task data in a public issue.
Use the repository Security tab to submit a private vulnerability report when this option is available.
Otherwise, contact the maintainer at `b@robitzs.ch`.
Include the affected version, host configuration, reproduction steps, and expected boundary.
Use synthetic data in the reproduction.

Security corrections target the latest source and package version.
The project does not promise support for older versions or a fixed response time.

## Trust boundary

Task definitions, Nix expressions, Compose bind sources, and `rootfs` inputs need trusted authors.
Nix evaluation runs with the host user's permissions before sandbox execution.
Do not accept these inputs from an untrusted task author.

Seat commands run in Linux namespaces with the task's declared filesystem binds.
Each writable host bind grants access to that host directory.
Do not bind credentials, private host data, or a container runtime socket into a seat.
The package does not supply application-specific authorization or validate task results.

## Transfers

A download process starts the package-owned helper in a read-only host root.
The writable sandbox tree arrives as read-only data, so the seat cannot name a loader file, module, or program for that process.
Links resolve inside the sandbox root. A file export may end at a regular file through that link resolution.
Each source descriptor receives a type check before any data read.
The helper refuses a directory export that contains a link.
It refuses a pipe, a device, and a socket before it reads any data, so a source without a writer cannot hold the transfer.
The host revalidates each private staged entry and publishes a fresh regular file or directory.
Below the selected destination the host follows no link.
It replaces an existing file entry and refuses an existing directory link.
A refusal publishes no downloaded entry, and that refusal leaves the existing destination contents unchanged.
A host I/O error during publication can leave partial output.
A transfer error names the refused path.
Use trusted task definitions for these runs, as for all other seat commands.

## Operational limits

- Host-network mode exposes host loopback services and abstract Unix sockets.
- The package sets no CPU or memory limits. It refuses enforcement modes that it cannot supply.
- Command and healthcheck output has no package byte limit.
- Commands from outside the Nix closure can lack a loader or library that they need.
- Sandbox security depends on the Linux kernel, bubblewrap, Nix, and the host configuration.
- A namespace test suite does not certify resistance to every hostile program.

Use a dedicated worker for untrusted seat code.
Use `network_mode: none` when a task does not need the host network.
Do not relax AppArmor controls on a shared host for this package.
Do not treat this backend as a replacement for a complete multi-tenant security policy.
