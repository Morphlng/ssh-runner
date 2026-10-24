"""Local checks for explicitly configured OpenSSH files; never connect."""

import os
import stat

from .._platform import WINDOWS, open_private, open_regular
from ..core.errors import ConfigError


def check_local_files(config):
    if WINDOWS and config.reuse_connection:
        raise ConfigError(
            "Windows OpenSSH 不支持连接复用；请设置 reuse_connection=false 或使用 --no-reuse。"
        )
    for field in (
        "identity_file",
        "certificate_file",
        "known_hosts_file",
        "ssh_config_file",
    ):
        path = getattr(config, field)
        if not path:
            continue
        try:
            fd = (
                open_private if WINDOWS and field == "identity_file" else open_regular
            )(path)
        except OSError:
            raise ConfigError(
                field + " must point to an existing readable file."
            ) from None
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise ConfigError(field + " must be a regular file.")
            if not WINDOWS and field == "identity_file" and info.st_mode & 0o077:
                raise ConfigError(
                    "identity_file permissions are too open; use chmod 600."
                )
        finally:
            os.close(fd)
