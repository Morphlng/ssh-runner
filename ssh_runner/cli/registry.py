"""Explicit command tree: the single source for parsing and discovery."""

import argparse
from pathlib import Path

from ..core.errors import ToolError
from .commands.execution import ExecuteCommand
from .commands.jobs import FIELDS, JobCommand
from .commands.management import (
    CapabilitiesCommand,
    ConfigCommand,
    ConnectionCommand,
    DoctorCommand,
)
from .commands.skills import SkillCommand
from .commands.transfers import TransferCommand


class Parser(argparse.ArgumentParser):
    def error(self, message):
        error = ToolError(message, kind="argument")
        error.operation = ".".join(self.prog.split()[1:]) or None
        raise error


def commands():
    return [
        ExecuteCommand(),
        ExecuteCommand(script=True),
        *[
            TransferCommand(directory, action)
            for directory in (False, True)
            for action in ("upload", "download")
        ],
        *[JobCommand(action) for action in FIELDS],
        *[ConnectionCommand(action) for action in ("status", "close")],
        *[ConfigCommand(action) for action in ("init", "check", "list")],
        DoctorCommand(),
        CapabilitiesCommand(),
        SkillCommand(),
    ]


def build_parser(registry):
    from .. import __version__

    parser = Parser(
        prog="ssh-runner",
        description="面向 AI Agent 的非交互 OpenSSH 控制工具",
        allow_abbrev=False,
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument(
        "--config",
        type=Path,
        help="私有 JSON 配置；默认 ~/.config/ssh-runner/config.json",
    )
    parser.add_argument("--server", help="命名服务")
    parser.add_argument("--format", choices=("json", "text"), default="json")
    groups = {(): parser.add_subparsers(dest="group", required=True)}
    for command in registry:
        prefix = command.path[:-1]
        if prefix and prefix not in groups:
            parent = groups[()].add_parser(prefix[0], allow_abbrev=False)
            groups[prefix] = parent.add_subparsers(dest="action", required=True)
        leaf = groups[prefix].add_parser(
            command.path[-1],
            help=command.help,
            description=command.help,
            allow_abbrev=False,
        )
        command.configure(leaf)
        leaf.set_defaults(handler=command)
    return parser
