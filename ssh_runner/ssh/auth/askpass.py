"""Internal OpenSSH askpass entry point; stdout is always a raw response."""

import os
import sys
from pathlib import Path

# This entry point also works from a source checkout and an arbitrary cwd.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from ssh_runner.config import ConfigStore
from ssh_runner.core.errors import ToolError
from ssh_runner.ssh.auth import SNAPSHOT_ENV, get_policy


def main():
    try:
        if len(sys.argv) != 2 or not os.environ.get(SNAPSHOT_ENV):
            raise ToolError("无效的内部认证调用。")
        _, config = ConfigStore(os.environ[SNAPSHOT_ENV]).select()
        print(
            get_policy(config.auth_mode).answer(
                config, sys.argv[1], os.environ.get("SSH_RUNNER_PROMPT_HOST")
            )
        )
        return 0
    except ToolError as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
