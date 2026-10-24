"""Module entry point: python3 -m ssh_runner [options]."""

import sys

from .cli import run

sys.exit(run())
