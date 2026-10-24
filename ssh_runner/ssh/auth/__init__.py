"""Explicit password challenge policies and the short-lived askpass environment."""

import os
import shlex
import site
import sys
import sysconfig
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Iterator, Optional

from ..._platform import WINDOWS, open_private, private_temporary_directory
from ...config import Config, serialize_config
from ...core.errors import ToolError
from .policies import get_policy

SNAPSHOT_ENV = "SSH_RUNNER_CONFIG_FILE"


def windows_askpass():
    scripts = Path(sysconfig.get_path("scripts"))
    # A --user installation has its launcher in the user scripts scheme, not
    # sys.prefix/Scripts. Choose it only when this package is in that user site.
    if Path(site.getusersitepackages()).resolve() in Path(__file__).resolve().parents:
        scripts = Path(sysconfig.get_path("scripts", scheme="nt_user"))
    helper = scripts / "ssh-runner-askpass.exe"
    if not helper.is_file():
        raise ToolError(
            "缺少当前 Python 环境的 ssh-runner-askpass.exe；请使用 pip install 或 editable install 安装。",
            kind="openssh",
        )
    return helper


@contextmanager
def askpass_environment(
    config: Config, prompt_host: Optional[str] = None
) -> Iterator[Dict[str, str]]:
    """Freeze credentials for this run; remove the snapshot and helper on exit."""
    with private_temporary_directory(prefix="ssh-runner-askpass-") as directory:
        snapshot = Path(directory) / "config.json"
        # Create with mode 0600 rather than tightening permissions after writing.
        fd = open_private(snapshot, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            # JSON preserves quotes, spaces and literal shell characters.
            handle.write(serialize_config(config))
        if WINDOWS:
            helper = windows_askpass()
        else:
            helper = Path(directory) / "askpass"
            entrypoint = Path(__file__).resolve().with_name("askpass.py")
            helper.write_text(
                '#!/bin/sh\nexec %s %s "$@"\n'
                % (shlex.quote(sys.executable), shlex.quote(str(entrypoint))),
                encoding="utf-8",
            )
            helper.chmod(0o700)
        environment = os.environ.copy()
        environment.pop("SSH_PASSWORD", None)
        environment.pop("SSHPASS", None)
        environment.update(
            {
                "SSH_ASKPASS": str(helper),
                "SSH_ASKPASS_REQUIRE": "force",
                "PYTHONIOENCODING": "utf-8",
                SNAPSHOT_ENV: str(snapshot),
                "SSH_RUNNER_PROMPT_HOST": prompt_host or config.host,
            }
        )
        yield environment
