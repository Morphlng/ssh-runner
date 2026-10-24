"""Private, credential-isolated OpenSSH masters shared across CLI invocations."""

import os
import stat
import time
from contextlib import suppress
from pathlib import Path
from typing import Optional

from .._platform import WINDOWS
from ..config import Config
from ..core.errors import ToolError
from ..core.results import ExecutionResult
from .state import ConnectionState, _locked
from .transport import OpenSSHTransport


class ConnectionManager(ConnectionState):
    """Only bootstrap is serialized. Command sessions remain independent."""

    def __init__(
        self, config: Config, *, control_dir: Optional[Path] = None, transport=None
    ):
        if WINDOWS:
            raise ToolError(
                "Windows OpenSSH 不支持连接复用；请设置 reuse_connection=false 或使用 --no-reuse。"
            )
        super().__init__(
            config,
            transport or OpenSSHTransport(config.ssh_binary),
            control_dir,
            multiplex=True,
        )
        # macOS has a 104-byte sockaddr_un.sun_path. Leave room for OpenSSH's
        # temporary socket suffix during atomic creation.
        if len(os.fsencode(str(self.control_path))) > 85:
            raise ToolError("SSH 控制 socket 路径过长；请使用 /tmp 下的较短控制目录。")

    def _socket_exists(self) -> bool:
        try:
            info = self.control_path.lstat()
        except FileNotFoundError:
            return False
        if (
            not stat.S_ISSOCK(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
        ):
            raise ToolError("SSH 控制 socket 类型、归属或权限不符合要求。")
        return True

    def _connected(self) -> bool:
        return self._socket_exists() and self.transport.control_command(
            self.config, self.control_path, "check"
        )

    def _close_if_present(self) -> None:
        with suppress(ToolError, OSError):
            if self._socket_exists():
                self.transport.control_command(self.config, self.control_path, "exit")

    def ensure_connected(self) -> Optional[ExecutionResult]:
        """Create at most one master; return startup failures without running a command."""
        with _locked(self.lock_path, self.config.connect_timeout):
            if self._connected():
                return None
            remaining = self._cooldown_remaining()
            if remaining:
                raise ToolError(
                    "上次 SSH 连接失败，请在 %d 秒后再试；本次未发起认证。" % remaining
                )
            if self._socket_exists():
                with suppress(FileNotFoundError):
                    self.control_path.unlink()
            try:
                result = self.transport.start_master(
                    self.config, self.control_path, prompt_host=self.prompt_host
                )
                if result.exit_code == 0 and self._connected():
                    with suppress(FileNotFoundError):
                        self.cooldown_path.unlink()
                    return None
                self._close_if_present()
                self._record_failure()
                if result.exit_code != 0:
                    return result
                return ExecutionResult(
                    255, False, "", "SSH 主连接未就绪，本次未发送远端命令。"
                )
            except BaseException:
                # Bootstrap may already have forked. Close a reachable master
                # in addition to the bootstrap process group's cleanup.
                self._close_if_present()
                with suppress(ToolError, OSError):
                    self._record_failure()
                raise

    def status(self) -> dict:
        with _locked(self.lock_path, self.config.connect_timeout):
            return {
                "connected": self._connected(),
                "host": self.config.host,
                "user": self.config.user,
                "port": self.config.port,
                "auth_mode": self.config.auth_mode,
                "control_path": str(self.control_path),
                "control_persist": self.config.control_persist,
            }

    def close(self) -> dict:
        with _locked(self.lock_path, self.config.connect_timeout):
            connected = self._connected()
            if connected and not self.transport.control_command(
                self.config, self.control_path, "exit"
            ):
                raise ToolError("关闭 SSH 主连接失败。")
            if not connected and self._socket_exists():
                with suppress(FileNotFoundError):
                    self.control_path.unlink()
            with suppress(FileNotFoundError):
                self.cooldown_path.unlink()
            return {"connected": False, "closed": connected}
