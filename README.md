# SSH Runner

Non-interactive SSH for AI agents.

Run commands, scripts, file transfers and background jobs on named servers through local OpenSSH, with structured JSON results. No third-party Python runtime dependencies.

[中文说明](README.zh-CN.md) · [Authentication](docs/authentication.md) · [Agent guide](AGENT_GUIDE.md) · [Contributing](CONTRIBUTING.md)

- SSH keys, ssh-agent, certificates, password and keyboard-interactive authentication.
- OpenSSH Host aliases, custom SSH configuration files and jump hosts.
- Strict host verification, bounded output, password redaction and atomic transfers.
- Cross-process ControlMaster reuse on macOS/Linux; concurrent direct connections on native Windows. Failed commands are never automatically replayed.
- macOS / Linux / Windows 10/11 x64 clients, Python 3.8+, OpenSSH 8.7+, with Linux/macOS remote hosts. See [Windows setup](docs/windows.md).

SSH Runner executes commands with the remote account's permissions. Use dedicated accounts and least privilege. See [SECURITY.md](SECURITY.md) for credential handling and security boundaries.

## Install

The initial public release is being prepared. Install from this checkout:

```bash
pipx install .
# Alternative: uv tool install .
# Or inside a virtual environment: python -m pip install .
ssh-runner capabilities
```

After version 0.1.0 is published to PyPI, use `pipx install ssh-runner`. See [INSTALL.md](INSTALL.md) for Git installation and upgrades.

## Quick start with an SSH alias

First establish and verify the host fingerprint using normal interactive SSH. Load encrypted keys into ssh-agent before unattended use. For an existing `Host remote-server` in `~/.ssh/config`:

```bash
ssh-runner config init
# Edit ~/.config/ssh-runner/config.json locally:
```

```json
{
  "version": 1,
  "default_server": "remote-server",
  "servers": {
    "remote-server": {"host": "remote-server", "auth_mode": "key"}
  }
}
```

```bash
chmod 600 ~/.config/ssh-runner/config.json
ssh-runner config list
ssh-runner --server remote-server config check
ssh-runner --server remote-server exec 'hostname; id; pwd' --timeout 30
```

`host` is passed unchanged to OpenSSH, so `HostName`, `User`, `Port`, `IdentityFile`, `ProxyJump` and trust settings come from the SSH configuration. Explicit `user` and `port` override those settings; leave them out to inherit. A file named `.ssh_config` is **not** an OpenSSH default: include it from `~/.ssh/config` or set `"ssh_config_file": "~/.ssh_config"` in the server profile. Only trusted SSH configuration files should be used.

The default application configuration is `~/.config/ssh-runner/config.json` on POSIX and `%LOCALAPPDATA%\ssh-runner\config.json` on Windows. A project-local file requires an explicit path:

```bash
cp -n ./config.json.example ./config.json
# Edit ./config.json locally to match the server name below.
chmod 600 ./config.json
ssh-runner --config ./config.json --server remote-server config check
ssh-runner --config ./config.json --server remote-server exec 'hostname'
```

The [example configuration](config.json.example) matches the built-in `config init` template. Adjust the server names and profiles locally; `cp -n` preserves an existing configuration. Private `config.json` files are ignored by Git.

`config check` validates the selected profile and explicitly configured local files without invoking SSH. It does not validate alias expansion, file references inside SSH configuration, credentials or host trust; use `doctor` for connection diagnostics.

## Explicit keys and authentication

```json
{
  "host": "server.example.com",
  "user": "deploy",
  "port": 22,
  "auth_mode": "key",
  "identity_file": "~/.ssh/id_ed25519",
  "identities_only": true
}
```

Optional `certificate_file` and `known_hosts_file` select an SSH certificate and a dedicated trust file. Empty file fields leave OpenSSH defaults intact. `identities_only` defaults to true when `identity_file` is supplied; otherwise it inherits OpenSSH settings. Paths expand `~`; relative paths are relative to the invoking process's working directory.

The generated template puts key authentication first. For compatibility, omitting `auth_mode` still selects `password`; always set the mode explicitly. Password modes require a `password` field. Passwords are stored as plaintext in the local private configuration at the user's own risk. Keyboard-interactive accepts an exact password challenge, never OTP, password changes or key passphrases. See [authentication details](docs/authentication.md).

## Commands and results

Global `--config`, `--server` and `--format` arguments go **before** the subcommand. Use `capabilities` and each command's `--help` for the current interface.

| Command | Purpose |
| --- | --- |
| `exec COMMAND`, `script PATH` | Commands, scripts, cwd, input files and bounded output |
| `file upload/download SOURCE DEST` | Verified binary file transfers |
| `directory upload/download SOURCE DEST` | Complete directories; destination must not exist |
| `job start COMMAND` | Background work with an explicit, recoverable ID |
| `job status/logs/wait/follow/cancel/delete ID`, `job list` | Task monitoring and cleanup |
| `connection status/close` | Shared master management |
| `config init/check/list` | Local configuration |
| `doctor`, `capabilities` | Connection diagnostics / local feature discovery |
| `skill install` | Register the bundled agent skill |

```bash
ssh-runner --server dev script ./check.sh --arg='two words' --cwd /srv/project
ssh-runner --server dev file upload ./patch.py /srv/project/patch.py --mode 644
ssh-runner --server dev job start 'make test' --job-id 0123456789abcdef0123456789abcdef --cwd /srv/project
ssh-runner --server dev job follow 0123456789abcdef0123456789abcdef
ssh-runner --format text --server dev exec 'pwd'
ssh-runner skill install --claude
```

Results use `schema_version: 1`. Check `exit_code`, `timed_out`, `error_kind` and truncation flags; remote stdout/stderr are separate from structured `data`. Each stream previews up to 65,536 bytes by default. `job follow` emits JSONL with `logs` and final `result` events. A successful `job start` result acknowledges the request; inspect task state and its eventual exit code. After a lost acknowledgement, query the original ID.

Timeouts and disconnects do not establish whether remote work stopped. Verify state and outputs before another attempt. Each call starts a separate shell; there is no PTY, interactive terminal, persistent cwd, port forwarding or sandbox. Directory transfers and jobs require remote POSIX Python 3.8+; other commands require the tools reported by `doctor`.

## Python API

```python
from ssh_runner import ConfigStore, SSHClient

name, config = ConfigStore().select("dev")
client = SSHClient(config)
result = client.execute("pwd", timeout=30)
result = client.script("./check.sh", args=["--verbose"])
result = client.files.upload("./file", "/srv/file")
```

Use `client.directories`, `client.jobs`, `client.connection` and `client.doctor()` for other operations. Shared masters survive client destruction until explicitly closed or idle expiry. Create a new client after configuration/environment changes. Close the connection or use `--no-reuse` after replacing key material at the same path.

MIT License — [LICENSE](LICENSE).
