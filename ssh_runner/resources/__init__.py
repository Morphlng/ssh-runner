"""Installed package resources and the single source skill during development."""

from importlib import resources
from pathlib import Path


def resource_text(name):
    return resources.read_text(__package__, name, encoding="utf-8")


def remote_source(name):
    return resources.read_text("ssh_runner.remote", name + ".py", encoding="utf-8")


def skill_directory():
    packaged = Path(__file__).resolve().parent / "skills" / "ssh-runner"
    if packaged.is_dir():
        return packaged
    source = Path(__file__).resolve().parents[2] / ".agents" / "skills" / "ssh-runner"
    if not (source / "SKILL.md").is_file():
        from ..core.errors import ToolError

        raise ToolError("安装包缺少 ssh-runner 技能资源，请重新安装。")
    return source
