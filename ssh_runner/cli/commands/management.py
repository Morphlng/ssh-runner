"""Configuration, connection and diagnostic commands."""

from pathlib import Path

from ...config.checks import check_local_files
from ...core.errors import ToolError
from ...core.results import ExecutionResult
from .base import Command, remote_arguments


def result(data):
    return ExecutionResult(0, False, "", "", data=data)


class ConfigCommand(Command):
    def __init__(self, action):
        self.action = action
        self.path = ("config", action)
        self.help = {
            "init": "创建私有配置模板",
            "check": "只校验本地配置",
            "list": "列出服务摘要",
        }[action]

    def run(self, context, options):
        return getattr(self, self.action)(context)

    def init(self, context):
        context.store.initialize()
        return result({"config_created": str(context.store.path)})

    def list(self, context):
        return result({"servers": context.catalog.summaries()})

    def check(self, context):
        name, config = context.selected
        check_local_files(config)
        return result(
            dict(
                config_valid=True,
                server=name,
                host=config.host,
                user=config.user,
                port=config.port,
                auth_mode=config.auth_mode,
                reuse_connection=config.reuse_connection,
                connection_mode="multiplex" if config.reuse_connection else "direct",
            )
        )


class ConnectionCommand(Command):
    def __init__(self, action):
        self.action = action
        self.path = ("connection", action)
        self.help = "查看共享连接状态" if action == "status" else "关闭共享连接"

    def configure(self, parser):
        parser.add_argument("--control-dir", type=Path)

    def run(self, context, options):
        data = getattr(context.client.connection, self.action)()
        data["server"] = context.selected[0]
        return result(data)


class DoctorCommand(Command):
    path = ("doctor",)
    help = "连接远端并检查配置、SSH 会话和工具依赖"
    requirements = "SSH exec"

    def configure(self, parser):
        remote_arguments(parser)

    def run(self, context, options):
        try:
            client = context.client
        except ToolError as error:
            return ExecutionResult(
                2,
                False,
                "",
                str(error),
                error_kind=error.kind,
                data={
                    "checks": [
                        {"stage": error.kind, "status": "failed", "detail": str(error)}
                    ]
                },
            )
        return client.doctor(timeout=options.timeout)


class CapabilitiesCommand(Command):
    path = ("capabilities",)
    help = "输出机器可读能力，无需配置或连接"

    def run(self, context, options):
        from ... import __version__
        from ...core.defaults import (
            DEFAULT_OUTPUT_BYTES,
            WINDOWS,
            default_reuse_connection,
        )
        from ...ssh.auth.policies import POLICIES

        return result(
            dict(
                version=__version__,
                schema_version=1,
                local_platform="windows" if WINDOWS else "posix",
                connection=dict(
                    reuse_supported=not WINDOWS,
                    default_mode=(
                        "multiplex" if default_reuse_connection() else "direct"
                    ),
                ),
                operations=[
                    dict(name=c.operation, requirements=c.requirements)
                    for c in context.commands
                ],
                auth_modes=list(POLICIES),
                default_max_output_bytes=DEFAULT_OUTPUT_BYTES,
                follow_format="jsonl",
                script=dict(arguments=True, interpreter="sh"),
                sudo=dict(command=True, stdin=False, password_field="sudo_password"),
            )
        )
