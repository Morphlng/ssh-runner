# Installation

Requirements: macOS / Linux / Windows 10/11 x64 clients, Python 3.8+, system OpenSSH 8.7+. Remote hosts are Linux/macOS. See [native Windows installation, ACLs and connection behavior](docs/windows.md). WSL remains a separate Linux environment with its own credentials.

## Source installation (available now)

```bash
git clone https://github.com/Morphlng/ssh-runner.git
cd ssh-runner
pipx install .
# Alternative: uv tool install .
```

Or create an isolated environment:

```bash
python3 -m venv ~/.local/share/ssh-runner/venv
~/.local/share/ssh-runner/venv/bin/python -m pip install .
~/.local/share/ssh-runner/venv/bin/ssh-runner capabilities
```

Use the executable's absolute path if its bin directory is not on PATH. From a checkout, `python3 -m ssh_runner capabilities` also works.

After the initial release is published, install with `pipx install ssh-runner`, `uv tool install ssh-runner`, or `python -m pip install ssh-runner` inside a virtual environment. To pin a Git install, use a verified full commit SHA with `pipx install 'git+https://github.com/Morphlng/ssh-runner.git@COMMIT_SHA'`.

## Agent skill

```bash
ssh-runner skill install
ssh-runner skill install --claude
ssh-runner skill install --scope project --project-dir /absolute/project --claude
```

Skills link to the installed package's bundled resources (directory junctions on Windows, symlinks on POSIX). Keep the environment location stable; moving or deleting it breaks those links. Existing conflicting files/directories are never overwritten. The canonical source is `.agents/skills/ssh-runner`; there is no second source copy for another agent.

## Configuration and upgrade

```bash
ssh-runner config init
# Edit credentials locally; never paste passwords into a chat.
ssh-runner --server dev config check
```

Use `--config /absolute/config.json` for any nondefault location. Initialization never overwrites an existing file or symlink. See [authentication](docs/authentication.md) for key paths and SSH aliases.

For source upgrades, install the reviewed checkout again using `pipx install --force .` or `uv tool install --force .`. For published versions use `pipx upgrade ssh-runner` or `uv tool upgrade ssh-runner`. Recheck capabilities and skill links. Running remote job workers are not replaced. Changing `job_root` does not move old jobs: query them with the original setting.
