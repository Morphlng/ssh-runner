# Native Windows clients

Windows 10/11 x64 can run SSH Runner directly with Python 3.8+ and native Windows OpenSSH 8.7+. Remote hosts remain Linux/macOS: commands use POSIX shell syntax; jobs and directories require remote POSIX Python 3.8+. No WSL, Bash, extra SSH library or administrator privileges are required for normal client use.

## Install and configure

From a reviewed checkout in PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install .
.\.venv\Scripts\ssh-runner.exe capabilities
.\.venv\Scripts\ssh-runner.exe config init
```

For development install `.[test,dev]` and `build` in a disposable environment with `pip install -e`. Password authentication requires the installed `ssh-runner-askpass.exe` in that same environment; an uninstalled checkout does not supply this launcher. pipx and uv tool installs also generate it.

The default config is `%LOCALAPPDATA%\ssh-runner\config.json`, with connection state in `%LOCALAPPDATA%\ssh-runner\state`. `--config` and `--control-dir` override these locations. The working directory never selects a config. Native Windows uses its own `%USERPROFILE%\.ssh\config`, known_hosts and ssh-agent, independently of WSL.

`config init` creates a UTF-8 file with a protected ACL before writing. Edit that file locally. Configs, credential snapshots and explicit private keys must belong to the current Windows user and grant access only to that user, SYSTEM and Administrators. `chmod 600` does not establish a Windows ACL. Broad ACLs, alternate streams and reparse points are rejected. Use a local ACL-capable filesystem (NTFS); UNC paths and device namespaces are outside the supported local-file scope.

JSON accepts forward slashes in local paths, such as `"identity_file": "C:/Users/alice/.ssh/id_ed25519"`; backslashes must be doubled. Remote paths remain `/srv/project`. Keep shell scripts as LF: their bytes are transmitted unchanged. Directory uploads use 0644 for files and 0755 for directories; downloads preserve contents and timestamps, without emulating POSIX permissions.

Verify host fingerprints with ordinary SSH first. Encrypted keys must already be unlocked in the native ssh-agent. Password modes only answer the configured ordinary login-password challenge; OTP, passphrases and password changes remain interactive tasks. Secrets never appear in SSH arguments or password-valued environment entries.

## Connection behavior

Windows defaults to `reuse_connection=false`. An explicit `true` fails before connecting; use `--no-reuse` for one invocation of a shared profile. Direct mode is selected before execution, never as a fallback after a multiplex failure. macOS/Linux retain their default ControlMaster reuse.

Each command or job query opens one SSH connection. Calls can run concurrently. SSH exit code 255 and timeouts conservatively record a cooldown; subsequent requests are blocked for `reconnect_delay` seconds (default 5). Already-running requests continue, and a concurrent success does not erase another request's failure. A remote command deliberately returning 255 also triggers this cooldown. Commands are never automatically retried or replayed.

`connection status` reports `mode=direct`, `connected=false`; `connection close` has no shared connection to close. Both remain local. `capabilities` reports platform/default mode without loading config. `doctor` checks local permissions, private storage, launcher and OpenSSH before probing the remote dependencies.

SSH and its helpers run in a Job Object. Timeout, interruption and parent exit clean up the local process tree. A local timeout or disconnect cannot prove remote execution stopped; query the original job ID or inspect artifacts before retrying. CLI JSON/JSONL is UTF-8, including redirected output.

Skill installation creates directory junctions to the canonical bundled skill. Developer Mode is unnecessary. Conflicting paths are never replaced; keep the installation environment in place so junctions remain valid.

## Release acceptance

Windows CI uses disposable localhost SSH peers and credentials; full POSIX suites run on Linux/macOS. Before releasing Windows changes, install `.[test]` in a disposable WSL Python environment, then run from the installed native Windows environment:

```powershell
python tests/verify_windows_posix.py --distribution Ubuntu-24.04 --linux-python /path/to/venv/bin/python
```

This starts a temporary WSL loopback Paramiko server and verifies POSIX commands, scripts, a sudo transport fixture, binary files, directories, job logs/follow/cancel and doctor. It does not install a system sshd, use account credentials or modify sudoers. Record platform versions and outcomes with the release checks. Windows remote hosts and Windows connection multiplexing are outside this release's scope.
