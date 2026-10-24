# Contributing

Bug reports and pull requests are welcome. Describe the observed behavior, expected result, platform, Python/OpenSSH versions and a minimal reproduction using fake credentials. Never include private configurations, host details, keys or logs containing secrets. Report security issues through [SECURITY.md](SECURITY.md).

## Development

Use macOS/Linux or native Windows 10/11 x64, Python 3.8+ and OpenSSH 8.7+. Windows virtualenv executables are in `Scripts`, not `bin`; see [Windows development and acceptance](docs/windows.md). The runtime depends only on the standard library and system OpenSSH; Paramiko is a test-only dependency.

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[test,dev]'
python -m isort . --check-only
python -m black . --check
python -m unittest discover -s tests -v
python -m build
python -m twine check dist/*
python tests/verify_distribution.py dist
```

Integration tests use ephemeral localhost SSH servers and Unix sockets with disposable credentials. Never use real credentials in tests. The distribution verifier installs wheel, sdist and a local Git fixture outside the checkout with temporary HOME directories. Optional integration tests skip if dependencies are absent; release checks must install them.

Store local test/build/scan logs, reports and downloaded validation tools under `.validation/` (for example `.validation/tests.log` and `.validation/tools/`). This directory is ignored by Git and excluded from source distributions.

Preserve Python 3.8 syntax and macOS/Linux/Windows behavior. Keep changes focused and add regression coverage for behavioral changes, especially authentication, isolation, host trust, transfers and process lifecycle. See [AGENTS.md](AGENTS.md) for architecture boundaries. Keep English/Chinese onboarding, skill references and configuration examples consistent.

## Release preparation

See [the release checklist](docs/releasing.md). Publishing to TestPyPI/PyPI and creating release tags are maintainer actions after verification. Never include private local `config.json`, `.ssh_config`, keys, internal planning documents or repository history from another project in a release.
