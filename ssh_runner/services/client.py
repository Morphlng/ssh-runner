"""Public facade composing services around a single injectable executor."""

import dataclasses
import shlex

from ..core.errors import ToolError
from .diagnostics import Diagnostics
from .directories import DirectoryTransfer
from .execution import CommandExecutor
from .files import FileTransfer
from .jobs import JobManager
from .protocol import RemoteProtocol


class SSHClient:
    def __init__(self, config, *, transport=None, control_dir=None, executor=None):
        self.executor = executor or CommandExecutor(
            config, transport=transport, control_dir=control_dir
        )
        self.config = self.executor.config
        protocol = RemoteProtocol(self.executor)
        self.files = FileTransfer(self.executor)
        self.directories = DirectoryTransfer(self.executor, self.files, protocol)
        self.jobs = JobManager(self.executor, protocol)
        self.diagnostics = Diagnostics(self.executor)

    @property
    def connection(self):
        return self.executor.connection

    def execute(self, command, **options):
        return dataclasses.replace(
            self.executor.execute(command, **options), operation="exec"
        )

    def script(self, path, *, args=(), **options):
        if any(not isinstance(value, str) or "\x00" in value for value in args):
            raise ToolError("脚本参数必须是无 NUL 的字符串。")
        command = "sh -s --" + "".join(" " + shlex.quote(value) for value in args)
        return dataclasses.replace(
            self.executor.execute(command, stdin_file=path, **options),
            operation="script",
        )

    def doctor(self, **options):
        return self.diagnostics.run(**options)
