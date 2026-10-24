"""Register the bundled skill without copying or overwriting user content."""

import os
from pathlib import Path

from ..core.errors import ToolError
from ..resources import skill_directory


class SkillInstaller:
    def install(self, *, scope="user", project_dir=None, claude=False):
        if scope not in {"user", "project"}:
            raise ToolError("scope 必须是 user 或 project。", kind="argument")
        if (scope == "project") != (project_dir is not None):
            raise ToolError(
                "项目安装需 --scope project --project-dir PATH；用户安装不接受项目目录。",
                kind="argument",
            )
        root = (
            Path(project_dir).expanduser().resolve()
            if project_dir is not None
            else Path.home()
        )
        if not root.is_dir():
            raise ToolError("安装根目录不存在：" + str(root))
        source = skill_directory().resolve()
        destination = root / ".agents" / "skills" / "ssh-runner"
        links = [(destination, source)]
        if claude:
            links.append((root / ".claude" / "skills" / "ssh-runner", destination))
        # Preflight all destinations before creating either link.
        for target, _ in links:
            if os.path.lexists(str(target)) and target.resolve() != source:
                raise ToolError("技能目标冲突，未覆盖：" + str(target))
        try:
            for target, referent in links:
                if target == source or os.path.lexists(str(target)):
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                target.symlink_to(os.path.relpath(str(referent), str(target.parent)))
        except OSError as error:
            raise ToolError("无法注册技能链接：" + str(error)) from error
        return dict(
            source=str(source), links=[str(target) for target, _ in links], scope=scope
        )
