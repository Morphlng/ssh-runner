from pathlib import Path

from ...core.errors import ToolError
from ...services.skills import SkillInstaller
from .base import Command
from .management import result


class SkillCommand(Command):
    path = ("skill", "install")
    help = "把完整技能注册到 .agents/skills，可同时注册 Claude Code"

    def configure(self, parser):
        parser.add_argument("--scope", choices=("user", "project"), default="user")
        parser.add_argument("--project-dir", type=Path)
        parser.add_argument("--claude", action="store_true")

    def validate(self, options):
        if (options.scope == "project") != (options.project_dir is not None):
            raise ToolError(
                "项目安装需 --scope project --project-dir PATH；用户安装不接受项目目录。",
                kind="argument",
            )

    def run(self, context, options):
        return result(
            SkillInstaller().install(
                scope=options.scope,
                project_dir=options.project_dir,
                claude=options.claude,
            )
        )
