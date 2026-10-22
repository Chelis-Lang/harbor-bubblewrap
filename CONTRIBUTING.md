# Contributing

## Reports and proposals

Use [GitHub issues](https://github.com/Chelis-Lang/harbor-bubblewrap/issues) for bug reports, questions, and feature proposals.
Include the operating system and the Python, Harbor, bubblewrap, and Nix versions.
Supply a small task that reproduces the problem.
Remove credentials and private data from reports and logs.
Use the [security policy](SECURITY.md) for security reports.

Discuss changes to sandbox boundaries or the supported Compose contract before implementation.
Keep one topic per branch.
Retain deterministic checks for consumer-visible behavior and security boundaries.

## Local checks

Use Python 3.12 or later.
Run this command from the repository root:

```sh
python3.12 tools/check.py
```

The command needs access to PyPI.
It installs pinned development tools into temporary environments.
It checks lint, formatting, distribution metadata, and the installed core package outside the source checkout.
It builds the wheel from the source distribution.
Successful distributions appear in `dist/`.
Temporary environments do not alter your existing Python environment.

For source edits, install the development dependencies:

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -e '.[harbor,test,dev]'
.venv/bin/python -m ruff format src tests tools
```

## Linux checks

Install the host tools and namespace permissions in the [usage guide](README.md#install).
Use a dedicated worker when the host needs a less restrictive namespace policy.
Run the namespace checks against a wheel outside the source checkout:

```sh
python3.12 tools/check.py
CHECK_DIR="$(mktemp -d)"
python3.12 -m venv "$CHECK_DIR/venv"
"$CHECK_DIR/venv/bin/python" -m pip install './dist/harbor_bubblewrap-0.1.1-py3-none-any.whl[harbor,test]'
cp -R tests "$CHECK_DIR/tests"
cd "$CHECK_DIR"
HELL_TEST_NAMESPACES=1 "$CHECK_DIR/venv/bin/python" -I -m pytest -q "$CHECK_DIR/tests"
```

These tests execute the packaged oracle tasks and negative controls with stock Harbor.
They make no model calls.
CI runs the core distribution check and these Linux checks on hosted Ubuntu workers.

## Public release preparation

Before public access, confirm the license, security contact, repository history, and current CI results.
Enable private vulnerability reports in the repository security settings when GitHub supports this setting.
Confirm that the Security tab offers a private report path.
Review each distribution before you attach it to a release.
Do not publish credentials, local environments, run output, or private task data.

The [change history](CHANGELOG.md) identifies the current candidate.
A package build does not publish a release or change repository visibility.

`LICENSE` retains the standard MIT text.
Two prose-check suppression comments mark its long sentences for this repository's writing checker.
Those comments do not change the license terms.
