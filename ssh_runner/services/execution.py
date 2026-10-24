"""Command validation and dispatch; submitted commands are never replayed."""

import math
import os
import shlex
import stat
import tempfile
from contextlib import ExitStack
from functools import cached_property
from pathlib import Path
from typing import Optional

from ..config import resolve_environment
from ..core.defaults import COMMAND_TIMEOUT, DEFAULT_OUTPUT_BYTES
from ..core.errors import ToolError
from ..core.results import ExecutionResult
from ..ssh.connection import ConnectionManager
from ..ssh.transport import OpenSSHTransport, prompt_host


def remote_path(path: str) -> str:
    if not isinstance(path, str) or not path or "\x00" in path:
        raise ToolError("远端路径必须是非空字符串，且不能包含 NUL。")
    return path if path.startswith("/") else "./" + path


class CommandExecutor:
    def __init__(self, config, *, transport=None, control_dir=None):
        self.config = config
        self.transport = transport or OpenSSHTransport(config.ssh_binary)
        self.control_dir = control_dir
        self._connection = None

    @cached_property
    def resolved_config(self):
        return resolve_environment(self.config)

    @property
    def connection(self):
        if self._connection is None:
            self._connection = ConnectionManager(
                self.resolved_config,
                control_dir=self.control_dir,
                transport=self.transport,
            )
        return self._connection

    def execute(
        self,
        command: str,
        timeout: float = COMMAND_TIMEOUT,
        *,
        cwd: Optional[str] = None,
        stdin_file=None,
        max_output_bytes: int = DEFAULT_OUTPUT_BYTES,
        stdout_file=None,
        stderr_file=None,
        _binary_stdout: bool = False,
        sudo: bool = False,
    ) -> ExecutionResult:
        """Run once; files stream to disk while each returned stream is bounded."""
        if not isinstance(command, str) or not command.strip() or "\x00" in command:
            raise ToolError("远端命令不能为空，且不能包含 NUL 字符。")
        if cwd is not None:
            command = "cd %s || exit\n%s" % (shlex.quote(remote_path(cwd)), command)
        config = self.resolved_config
        if any(secret in command for secret in config.secrets):
            raise ToolError("远端命令包含配置的密码，已拒绝执行。")
        if type(sudo) is not bool:
            raise ToolError("sudo 必须是布尔值。")
        if sudo and stdin_file is not None:
            raise ToolError(
                "sudo 模式不支持 stdin_file；命令的 stdin 固定为 /dev/null。"
            )
        if (
            not isinstance(timeout, (float, int))
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise ToolError("timeout 必须是大于零的有限秒数。")
        if (
            type(max_output_bytes) is not int
            or not 0 <= max_output_bytes <= 16 * 1024 * 1024
        ):
            raise ToolError(
                "max_output_bytes 必须是 0 到 16777216 之间的整数（每条流）。"
            )
        with ExitStack() as stack:
            stdin = stdin_file
            if sudo:
                # Always detach the target command's stdin from the authentication
                # stream, including NOPASSWD/cached policies that consume no password.
                body = "exec </dev/null\n" + command
                if config.sudo_password:
                    stdin = stack.enter_context(tempfile.TemporaryFile())
                    stdin.write(config.sudo_password.encode("utf-8") + b"\n")
                    stdin.seek(0)
                    command = "sudo -S -k -p '' -- sh -c " + shlex.quote(body)
                else:
                    command = "sudo -n -- sh -c " + shlex.quote(body)
            if isinstance(stdin, (str, Path)):
                try:
                    stdin = stack.enter_context(
                        os.fdopen(
                            os.open(str(stdin), os.O_RDONLY | os.O_NONBLOCK), "rb"
                        )
                    )
                    if not stat.S_ISREG(os.fstat(stdin.fileno()).st_mode):
                        raise ToolError("stdin_file 必须是普通文件。")
                except OSError:
                    raise ToolError("无法读取本地输入文件。") from None
            kwargs = dict(
                stdin=stdin,
                max_output_bytes=max_output_bytes,
                stdout_file=stdout_file,
                stderr_file=stderr_file,
                binary_stdout=_binary_stdout,
            )
            if config.reuse_connection:
                manager = self.connection
                failure = manager.ensure_connected()
                if failure is not None:
                    return failure
                return self.transport.run_command(
                    manager.config, command, timeout, manager.control_path, **kwargs
                )
            config, effective_settings = self.transport.resolve_config(config)
            return self.transport.run_command(
                config,
                command,
                timeout,
                prompt_host=prompt_host(config, effective_settings),
                **kwargs,
            )
