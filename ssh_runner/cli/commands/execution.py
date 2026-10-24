"""Command and script execution adapters."""

import sys

from ...core.errors import ToolError
from .base import Command, output_arguments, remote_arguments, values


class ExecuteCommand(Command):
    def __init__(self, script=False):
        self.script = script
        self.path = ("script" if script else "exec",)
        self.help = "执行本地 shell 脚本" if script else "执行远端 shell 命令"
        self.requirements = "sh" if script else "SSH exec"

    def configure(self, parser):
        parser.add_argument(
            "source",
            help="本地脚本路径（- 表示 stdin）" if self.script else "远端命令字符串",
        )
        remote_arguments(parser)
        output_arguments(parser)
        parser.add_argument("--cwd", help="远端字面工作目录")
        if self.script:
            parser.add_argument(
                "--arg",
                action="append",
                default=[],
                help="字面脚本参数，可重复；前导短横线使用 --arg=VALUE",
            )
        else:
            parser.add_argument("--stdin-file", help="本地输入文件，- 表示 stdin")
            parser.add_argument(
                "--sudo",
                action="store_true",
                help="使用配置的 sudo_password；不支持目标命令 stdin",
            )

    def validate(self, options):
        if self.script:
            if any("\x00" in value for value in options.arg):
                raise ToolError("脚本参数不能包含 NUL。", kind="argument")
        elif options.sudo and options.stdin_file is not None:
            raise ToolError("--sudo 不能与 --stdin-file 同时使用。", kind="argument")

    def run(self, context, options):
        common = values(
            options, "cwd", "timeout", "max_output_bytes", "stdout_file", "stderr_file"
        )
        if self.script:
            path = sys.stdin.buffer if options.source == "-" else options.source
            return context.client.script(path, args=options.arg, **common)
        stdin = sys.stdin.buffer if options.stdin_file == "-" else options.stdin_file
        return context.client.execute(
            options.source, stdin_file=stdin, sudo=options.sudo, **common
        )
