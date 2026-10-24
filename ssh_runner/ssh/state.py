"""Credential-isolated state and cooldown shared by direct and multiplex modes."""

import dataclasses
import hmac
import json
import math
import os
import time
from contextlib import contextmanager
from pathlib import Path

from .._platform import WINDOWS, lock_file, open_private, private_directory
from ..config import resolve_environment
from ..core.defaults import default_control_dir
from ..core.errors import ToolError
from .transport import prompt_host


def _private_directory(path):
    try:
        private_directory(path, parents=WINDOWS)
    except OSError:
        raise ToolError(
            "SSH 状态目录必须是真实私有目录，且归当前用户所有 (0700 / restricted ACL)。"
        ) from None


def _private_fd(path):
    try:
        return open_private(path, os.O_RDWR | os.O_CREAT)
    except OSError:
        raise ToolError("SSH 状态文件必须为当前用户拥有的私有普通文件。") from None


@contextmanager
def _locked(path, timeout):
    fd = _private_fd(path)
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                lock_file(fd)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ToolError("等待 SSH 连接管理锁超时；未发起重复认证。")
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)


class ConnectionState:
    def __init__(self, config, transport, control_dir=None, *, multiplex=False):
        self.transport = transport
        config = resolve_environment(config)
        self.config, effective_settings = transport.resolve_config(config)
        self.prompt_host = prompt_host(self.config, effective_settings)
        self.directory = Path(
            os.path.abspath(str(control_dir or default_control_dir()))
        )
        if multiplex and (
            "%" in str(self.directory) or any(c.isspace() for c in str(self.directory))
        ):
            raise ToolError("SSH 控制目录不能包含空白字符或 OpenSSH 的 % 占位符。")
        _private_directory(self.directory)
        with _locked(self.directory / "pool.lock", config.connect_timeout):
            with os.fdopen(_private_fd(self.directory / "pool.key"), "r+b") as handle:
                key = handle.read(33)
                if not key:
                    key = os.urandom(32)
                    handle.write(key)
                    handle.flush()
                elif len(key) != 32:
                    raise ToolError("SSH 控制目录中的隔离密钥格式错误。")
        identity = json.dumps(
            {
                "config": {
                    k: v
                    for k, v in dataclasses.asdict(self.config).items()
                    if k
                    not in {"ssh_binary", "remote_python", "job_root", "sudo_password"}
                },
                "ssh_binary": transport.binary,
                "effective_settings": effective_settings,
            },
            sort_keys=True,
        ).encode("utf-8")
        token = hmac.new(key, identity, "sha256").hexdigest()[:24]
        self.control_path = self.directory / ("c-%s.sock" % token)
        self.lock_path = self.directory / ("c-%s.lock" % token)
        self.cooldown_path = self.directory / ("c-%s.cooldown" % token)

    def _cooldown_remaining(self):
        # lexists includes broken redirects, which open_private must reject.
        if not os.path.lexists(self.cooldown_path):
            return 0
        with os.fdopen(
            _private_fd(self.cooldown_path), "r", encoding="ascii"
        ) as handle:
            try:
                until = float(handle.read(64))
            except (ValueError, UnicodeError):
                raise ToolError("SSH 连接冷却记录格式错误。") from None
        if not math.isfinite(until):
            raise ToolError("SSH 连接冷却记录格式错误。")
        return max(0, math.ceil(until - time.time()))

    def check_cooldown(self):
        remaining = self._cooldown_remaining()
        if remaining:
            raise ToolError(
                "上次 SSH 连接失败，请在 %d 秒后再试；本次未发起认证。" % remaining
            )

    def _record_failure(self):
        with os.fdopen(
            _private_fd(self.cooldown_path), "w", encoding="ascii"
        ) as handle:
            handle.truncate(0)
            handle.write(str(time.time() + self.config.reconnect_delay))


class DirectConnection:
    """Concurrent one-shot sessions. Only state checks/updates take the lock."""

    def __init__(self, config, transport, control_dir=None):
        self.config, self.transport, self.control_dir = config, transport, control_dir

    def execute(self, command, timeout, **kwargs):
        state = ConnectionState(self.config, self.transport, self.control_dir)
        with _locked(state.lock_path, state.config.connect_timeout):
            state.check_cooldown()
        result = self.transport.run_command(
            state.config, command, timeout, prompt_host=state.prompt_host, **kwargs
        )
        if result.exit_code == 255 or result.timed_out:
            with _locked(state.lock_path, state.config.connect_timeout):
                state._record_failure()
        # A concurrent success must not erase another request's failure record.
        return result

    def status(self):
        return dict(
            connected=False,
            mode="direct",
            reuse_supported=not WINDOWS,
            host=self.config.host,
            user=self.config.user,
            port=self.config.port,
            auth_mode=self.config.auth_mode,
            control_path=None,
            control_persist=None,
        )

    def close(self):
        return dict(connected=False, closed=False, mode="direct")
