"""Shared defaults; protocol/platform constants stay with their implementation."""

import os
from pathlib import Path

DEFAULT_OUTPUT_BYTES = 65536
COMMAND_TIMEOUT = 30
JOB_TIMEOUT = 3600
LOG_LIMIT = 10 * 1024 * 1024
PAGE_LIMIT = 16384
WAIT_TIMEOUT = 300
POLL_INTERVAL = 1
STOP_GRACE_SECONDS = 2


def default_config_path():
    return Path.home() / ".config" / "ssh-runner" / "config.json"


def default_control_dir():
    # Keep paths short enough for macOS sockaddr_un and OpenSSH's suffix.
    return Path("/tmp") / ("ssh-runner-%d" % os.getuid())
