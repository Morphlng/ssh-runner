# SSH Runner maintenance

The main branch contains ssh-runner: an Agent-oriented, non-interactive OpenSSH client. Keep macOS/Linux and Python 3.8+ support. Runtime code uses only the standard library and system OpenSSH. Optional test/build dependencies are not runtime dependencies.

## Boundaries

- `core`: result models, operational errors, shared defaults. No dependency on CLI or services.
- `config`: immutable target models and private configuration IO.
- `ssh`: OpenSSH transport, connection pool, process lifecycle, authentication policies. Process IO must not import transport.
- `services`: compose the shared executor; never print or import CLI. SSHClient is the public facade, not an ancestor of every service.
- `cli/commands`: command groups own argparse declarations and service calls. The explicit registry drives both parsing and capabilities.
- `remote`: standalone stdlib scripts sent to remote Python. Do not add relative imports or require installing ssh-runner remotely. Keep existing worker/job storage readable.
- `resources`: templates and runtime loaders. The canonical skill is `.agents/skills/ssh-runner`; the build hook includes it in wheels. Do not maintain a second copied skill in source.

Use composition and narrow injected dependencies. Keep stateless transformations as functions. Catch expected failures at IO/protocol/CLI boundaries; retain resource cleanup, but do not catch arbitrary programming errors and pretend an operation succeeded.

## Extending behavior

Add a command class in the relevant command group and register it once; no central action switch. Add authentication differences to a policy and its registry. Add business capabilities as services sharing the executor. Runtime/platform differences belong in configuration or named constants, not scattered literals. Keep configuration selection independent from opening SSH connections.

Preserve credential isolation, strict host verification, no command replay, no multiplex fallback, bounded output, atomic transfers, job IDs and uncertain states. Changes to these behaviors need regression coverage, not just import or method-shape tests.

## Validation

Use a disposable virtual environment and `python -m pip install -e '.[test,dev]' build`. Run `python -m unittest discover -s tests -v`; full integration tests bind only localhost and Unix sockets. Then build wheel/sdist and test installed CLI/resources/skill links from outside the checkout. Use temporary HOME and project directories when testing skill registration. Never use real credentials for tests.

Run isort and Black with pyproject.toml settings. Verify Python 3.8 compatibility and macOS/Linux behavior; state explicitly when a platform or optional integration suite could not be exercised.
