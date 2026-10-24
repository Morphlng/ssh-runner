"""Collect the canonical Agent skill into wheels without duplicating sources."""

import shutil
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py


class BuildPy(build_py):
    def run(self):
        super().run()
        source = Path(__file__).parent / ".agents" / "skills" / "ssh-runner"
        target = (
            Path(self.build_lib) / "ssh_runner" / "resources" / "skills" / "ssh-runner"
        )
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(source, target)


setup(cmdclass={"build_py": BuildPy})
