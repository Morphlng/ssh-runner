# Authentication and SSH aliases

All examples are server profiles nested under `servers` in a version-1 application configuration. Configuration is private (POSIX mode 0600, or a restricted Windows ACL; see [Windows](windows.md)). It is never discovered in the current directory: pass `--config ./config.json` explicitly.

## Existing OpenSSH alias

Given a trusted OpenSSH configuration:

```sshconfig
Host aliyun
    HostName server.example.com
    User deploy
    Port 2222
    IdentityFile ~/.ssh/id_ed25519
    IdentitiesOnly yes
```

Use this profile:

```json
{"host": "aliyun", "auth_mode": "key"}
```

OpenSSH resolves the alias. Omitted/null `user` and `port` inherit its effective settings (then OpenSSH's local-user/22 defaults); explicit fields override them. The alias stays in SSH argv so Host matching, ProxyJump, HostKeyAlias and related routing remain native OpenSSH behavior. Effective settings participate in the connection fingerprint.

OpenSSH normally reads `~/.ssh/config`, not `~/.ssh_config`. Either Include the latter from your normal SSH config, or select it explicitly:

```json
{"host": "aliyun", "auth_mode": "key", "ssh_config_file": "~/.ssh_config"}
```

`ssh_config_file` maps to `ssh -F`; the custom file replaces the usual user file and follows OpenSSH's system-config/Include behavior. Configure needed includes yourself. It does not change host verification or permit arbitrary CLI SSH options. Config files and their includes/proxies are trusted executable local input.

Native Windows uses its own SSH config and agent, and supports installed askpass launchers for password modes. With WSL, use Linux paths and Linux SSH configuration/agent/known_hosts. Windows SSH configuration is not inherited automatically. Keep private JSON and keys inside the Linux home filesystem when mounted Windows files cannot enforce owner-only permissions.

## Explicit identity and certificate

```json
{
  "host": "server.example.com",
  "user": "deploy",
  "auth_mode": "key",
  "identity_file": "~/.ssh/id_ed25519",
  "identities_only": true,
  "certificate_file": "~/.ssh/id_ed25519-cert.pub",
  "known_hosts_file": "~/.ssh/known_hosts"
}
```

| Field | Behavior |
| --- | --- |
| `identity_file` | `ssh -i PATH`; empty inherits OpenSSH identities/agent |
| `identities_only` | true with an explicit identity by default; otherwise inherits OpenSSH; explicit false is supported |
| `certificate_file` | `CertificateFile=PATH`; empty inherits OpenSSH defaults |
| `known_hosts_file` | `UserKnownHostsFile=PATH`; strict checking always remains enabled |
| `ssh_config_file` | `ssh -F PATH`; empty uses normal OpenSSH configuration |

Paths expand `~` and become absolute at configuration load. Relative paths are relative to the process working directory. Literal spaces/quotes are supported; NUL, line breaks, OpenSSH `%`/`${...}` expressions and oversized strings are rejected. `config check` verifies explicitly configured files exist and are readable regular files, with no group/world permissions for a private identity. It does not parse SSH config or validate cryptographic contents/referenced files. Key mode never creates a password snapshot or answers private-key passphrases. Use ssh-agent to unlock encrypted keys.

After replacing key/certificate contents at the same path, close the old master or use `--no-reuse`. The default remote job directory is `~/.ssh-runner-jobs`; to query tasks stored elsewhere, explicitly use that original `job_root`.

## Password compatibility

```json
{"host": "server.example.com", "user": "alice", "auth_mode": "password", "password": ""}
```

Fill the password locally. Omitted `auth_mode` remains `password` for compatibility, so always specify it. For PAM password challenges use `keyboard-interactive`. It accepts `Password:` (including the matching `(user@resolved-host)` wrapper); custom ordinary password labels such as `Enter password:` can be set with `password_prompt`. The password method accepts the exact `user@resolved-host's password:` prompt. Unrecognized prompts, OTP, verification codes, key passphrases and password changes require interactive SSH. No credentials are guessed or retried.

`sudo_password` is independent of the SSH password, used only with `exec --sudo`, through stdin. Prefer narrowly scoped server sudoers rules. Plaintext configuration and same-user agent access are described in [SECURITY.md](../SECURITY.md).
