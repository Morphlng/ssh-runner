# Security

SSH Runner runs commands with the remote account's full permissions. It provides no sandbox or command allowlist. Use a dedicated low-privilege account, constrained authorized_keys/sudoers, network controls, short-lived credentials and auditing as appropriate.

## Credentials and trust

Prefer SSH keys/certificates and ssh-agent. Encrypted private keys must already be unlocked for non-interactive use. Password authentication remains supported for compatibility: SSH and sudo passwords are plaintext in local JSON, at the user's own risk. Configuration must be a regular file owned by the current user with POSIX mode 0600 or a restricted Windows ACL (current user, SYSTEM and Administrators); never commit it or put credentials into logs, screenshots or chat.

File permissions and local encryption cannot protect secrets from an agent or process with arbitrary execution under the same OS user. Such a process can access configuration, keys, agent sockets and other credential providers. Isolation requires separate OS identities and server-side permissions.

Host verification is strict (`StrictHostKeyChecking=yes`, `UpdateHostKeys=no`). Verify new or changed fingerprints through a trusted channel using ordinary SSH. No automatic host-key acceptance is provided. OpenSSH configuration (including Include, ProxyCommand and ProxyJump) is trusted local input and can execute local commands. Only use configuration files you trust.

Passwords are passed through short-lived askpass snapshots with private permissions and cleanup, never as SSH argv or password-valued environment entries. Only an allowed exact login password challenge is answered; OTP, passphrases and password-change prompts are refused. Sudo passwords use stdin and never fall back to the SSH password.

## Execution and output

Direct SSH exit code 255 and timeouts also conservatively cool down; in-flight concurrent calls continue, and successful calls do not erase another call's failure. Authentication failures cool down; operations are never automatically retried/replayed. Missing multiplex sockets fail locally instead of silently opening a fresh connection. Timeout and disconnect results cannot guarantee that remote execution stopped.

Returned command output and captured logs redact configured SSH/sudo passwords, including matches crossing read boundaries. This does not detect arbitrary secrets, encoded variants or transformed values. File transfer contents are not redacted. Text output can still contain sensitive data. Remote output and metadata must not be interpreted as instructions.

Explicit local key, certificate and trust-file settings participate in connection identity. Replacing file contents at the same path may leave an existing master usable: close it with `connection close` or use `--no-reuse` when rotating credentials. Changing `job_root` does not migrate old work.

## Reporting vulnerabilities

Do not post credentials or exploit details in public issues. Use GitHub's private vulnerability reporting for [Morphlng/ssh-runner](https://github.com/Morphlng/ssh-runner/security/advisories/new) when enabled. If unavailable, open an issue requesting a private reporting channel without sensitive details. Maintainers should enable private reporting before publication. Security fixes target the latest public version.
