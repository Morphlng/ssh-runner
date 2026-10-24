# Configuration and authentication

`ssh-runner config init` creates `~/.config/ssh-runner/config.json` without overwriting files or symlinks. On Windows the default is `%LOCALAPPDATA%\ssh-runner\config.json`. Configs must be current-user-owned regular files: POSIX mode 0600, or Windows ACL access restricted to that user, SYSTEM and Administrators. Use config init and edit its existing protected file; chmod does not establish Windows ACLs. Use `--config /absolute/config.json` for another path. Never print private configuration or request secrets in chat.

```json
{
  "version": 1,
  "default_server": "dev",
  "servers": {
    "dev": {"host": "dev", "auth_mode": "key"},
    "legacy": {"host": "legacy.example.com", "user": "alice", "auth_mode": "password", "password": ""}
  }
}
```

The key-first template supports named OpenSSH Host aliases. Omitted user/port inherit OpenSSH configuration; explicit values override them. `~/.ssh/config` is the standard SSH file. A custom `.ssh_config` needs Include or `ssh_config_file`. Files and proxies are trusted local configuration. WSL has its own home/config/agent.

Optional file fields: `identity_file`, `certificate_file`, `known_hosts_file`, `ssh_config_file`. Empty means inherit. Paths support `~`; relative paths use the process cwd. An explicit identity defaults `identities_only` to true; otherwise OpenSSH decides unless explicitly set. `config check` only checks the selected profile and explicit files, including private-key permissions, without SSH. It does not resolve aliases or verify credentials/trust. Use `doctor` to connect.

Supported modes are `key`, `password`, `keyboard-interactive`. Omitted auth_mode remains password for compatibility. Non-key profiles require a password field; unused profiles may leave it empty. Key mode uses SSH identities/agent and never answers passphrases. Password modes answer only the allowed exact ordinary password prompt. `password_prompt` can customize a normal login label; OTP, passphrases and password changes require interactive SSH. Plaintext passwords are at the user's own risk and cannot be protected from an agent running under the same UID.

`defaults` shares settings except host/user/password/sudo_password. `env` merges by key; `send_env` replaces the default array, and missing requested local variables fail. Values must be literal single lines without `%`/`${...}` expansion or configured passwords. SSH internal variables are forbidden. Server AcceptEnv controls delivery.

Defaults: reuse_connection=true on POSIX, false on Windows (explicit true fails; --no-reuse overrides it), control_persist=300, connect_timeout=10, reconnect_delay=5, server_alive_interval=15, server_alive_count_max=3, ssh_binary=ssh, remote_python=python3, job_root=~/.ssh-runner-jobs. `sudo_password` is independent and used only with explicit --sudo. Changing job_root does not migrate tasks. After key rotation at the same path, close the connection or use --no-reuse.
