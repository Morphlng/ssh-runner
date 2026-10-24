# Installation

Use the installed `ssh-runner` or `python -m ssh_runner` in the same environment. For a source checkout use `pipx install .` or an isolated virtual environment with `python -m pip install .`. The public repository is https://github.com/Morphlng/ssh-runner. Do not assume a release exists on PyPI until confirmed.

Run `ssh-runner capabilities`, then `ssh-runner skill install` (optionally `--claude`). Project registration uses `--scope project --project-dir /absolute/project`. Existing conflicting paths are not overwritten. Keep the installation environment stable because registered skills are links to bundled resources.

Requirements: macOS/Linux, Python 3.8+, OpenSSH 8.7+; Windows requires WSL with Linux credentials/configuration. Use `--config` for nondefault JSON paths. Do not modify shell startup files or install credentials automatically.
