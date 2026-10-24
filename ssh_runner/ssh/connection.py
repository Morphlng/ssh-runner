"""Private, credential-isolated OpenSSH masters shared across CLI invocations."""

import dataclasses
import fcntl
import hmac
import json
import math
import os
import stat
import time
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Iterator, Optional

from ..config import Config, resolve_environment
from ..core.defaults import default_control_dir
from ..core.errors import ToolError
from ..core.results import ExecutionResult
from .transport import OpenSSHTransport, prompt_host


def _private_directory(path: Path) -> None:
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    except OSError:
        raise ToolError("无法创建 SSH 控制目录；其父目录必须已存在且可写。") from None
    try:
        info = path.lstat()
    except OSError:
        raise ToolError("无法读取 SSH 控制目录。") from None
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
    ):
        raise ToolError("SSH 控制目录必须是当前用户拥有的真实目录，权限为 0700。")


def _private_fd(path: Path) -> int:
    try:
        fd = os.open(
            str(path), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600
        )
    except OSError:
        raise ToolError("无法打开 SSH 控制状态文件。") from None
    info = os.fstat(fd)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
    ):
        os.close(fd)
        raise ToolError("SSH 控制状态文件必须归当前用户所有，且其他用户无权限。")
    return fd


@contextmanager
def _locked(path: Path, timeout: float) -> Iterator[None]:
    fd = _private_fd(path)
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ToolError("等待 SSH 连接管理锁超时；未发起重复认证。")
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)


class ConnectionManager:
    """Only bootstrap is serialized. Command sessions remain independent."""

    def __init__(
        self, config: Config, *, control_dir: Optional[Path] = None, transport=None
    ):
        self.transport = transport or OpenSSHTransport(config.ssh_binary)
        config = resolve_environment(config)
        self.config, effective_settings = self.transport.resolve_config(config)
        self.prompt_host = prompt_host(self.config, effective_settings)
        self.directory = Path(
            os.path.abspath(str(control_dir or default_control_dir()))
        )
        if "%" in str(self.directory) or any(c.isspace() for c in str(self.directory)):
            raise ToolError("SSH 控制目录不能包含空白字符或 OpenSSH 的 % 占位符。")
        _private_directory(self.directory)
        with _locked(self.directory / "pool.lock", config.connect_timeout):
            fd = _private_fd(self.directory / "pool.key")
            with os.fdopen(fd, "r+b") as handle:
                key = handle.read(33)
                if not key:
                    key = os.urandom(32)
                    handle.write(key)
                    handle.flush()
                elif len(key) != 32:
                    raise ToolError("SSH 控制目录中的隔离密钥格式错误。")
        # A keyed fingerprint distinguishes passwords without publishing a
        # guessable password hash in a socket filename or saving credentials.
        identity = json.dumps(
            {
                "config": {
                    k: v
                    for k, v in dataclasses.asdict(self.config).items()
                    if k
                    not in {"ssh_binary", "remote_python", "job_root", "sudo_password"}
                },
                "ssh_binary": self.transport.binary,
                "effective_settings": effective_settings,
            },
            sort_keys=True,
        ).encode("utf-8")
        token = hmac.new(key, identity, "sha256").hexdigest()[:24]
        self.control_path = self.directory / ("c-%s.sock" % token)
        self.lock_path = self.directory / ("c-%s.lock" % token)
        self.cooldown_path = self.directory / ("c-%s.cooldown" % token)
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

    def _cooldown_remaining(self) -> int:
        if not self.cooldown_path.exists():
            return 0
        fd = _private_fd(self.cooldown_path)
        with os.fdopen(fd, "r", encoding="ascii") as handle:
            try:
                until = float(handle.read(64))
            except (ValueError, UnicodeError):
                raise ToolError("SSH 连接冷却记录格式错误。") from None
        if not math.isfinite(until):
            raise ToolError("SSH 连接冷却记录格式错误。")
        return max(0, math.ceil(until - time.time()))

    def _record_failure(self) -> None:
        fd = _private_fd(self.cooldown_path)
        with os.fdopen(fd, "w", encoding="ascii") as handle:
            handle.truncate(0)
            handle.write(str(time.time() + self.config.reconnect_delay))

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
