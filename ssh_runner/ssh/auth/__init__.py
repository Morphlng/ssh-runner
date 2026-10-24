"""Explicit password challenge policies and the short-lived askpass environment."""

import os
import shlex
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Iterator, Optional

from ...config import Config, serialize_config
from .policies import get_policy

SNAPSHOT_ENV = "SSH_RUNNER_CONFIG_FILE"


@contextmanager
def askpass_environment(
    config: Config, prompt_host: Optional[str] = None
) -> Iterator[Dict[str, str]]:
    """Freeze credentials for this run; remove the snapshot and helper on exit."""
    with tempfile.TemporaryDirectory(prefix="ssh-runner-askpass-") as directory:
        snapshot = Path(directory) / "config.json"
        # Create with mode 0600 rather than tightening permissions after writing.
        fd = os.open(str(snapshot), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            # JSON preserves quotes, spaces and literal shell characters.
            handle.write(serialize_config(config))
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
