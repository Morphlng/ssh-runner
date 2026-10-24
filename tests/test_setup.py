"""Register bundled skills with stable links and no destructive overwrites."""

import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ssh_runner import ToolError
from ssh_runner.resources import skill_directory
from ssh_runner.services.skills import SkillInstaller


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ssh skill ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / "package" / "ssh-runner"
        shutil.copytree(skill_directory(), self.source)
        self.home = self.root / "home"
        self.home.mkdir()
        patch = mock.patch(
            "ssh_runner.services.skills.skill_directory", return_value=self.source
        )
        patch.start()
        self.addCleanup(patch.stop)

    def install(self, **kwargs):
        return SkillInstaller().install(
            scope="project", project_dir=self.home, **kwargs
        )

    def test_agents_share_full_skill_and_repeated_install_preserves_links(self):
        self.install(claude=True)
        primary = self.home / ".agents/skills/ssh-runner"
        claude = self.home / ".claude/skills/ssh-runner"
        before = primary.lstat().st_ino
        self.install(claude=True)
        self.assertEqual(primary.lstat().st_ino, before)
        self.assertEqual(primary.resolve(), self.source)
        self.assertEqual(claude.resolve(), self.source)
        for document in self.source.rglob("*.md"):
            for reference in re.findall(r"\]\(([^)#]+)", document.read_text()):
                if "://" not in reference:
                    self.assertTrue(
                        (document.parent / reference).is_file(), (document, reference)
                    )

    def test_conflicts_are_preflighted_without_creating_primary_link(self):
        target = self.home / ".claude/skills/ssh-runner"
        target.parent.mkdir(parents=True)
        for kind in ("file", "directory", "wrong-link", "broken-link"):
            with self.subTest(kind=kind):
                if kind == "file":
                    target.write_text("keep")
                elif kind == "directory":
                    target.mkdir()
                else:
                    target.symlink_to(
                        self.home if kind == "wrong-link" else self.home / "missing"
                    )
                before = target.lstat().st_ino
                with self.assertRaises(ToolError):
                    self.install(claude=True)
                self.assertEqual(target.lstat().st_ino, before)
                self.assertFalse((self.home / ".agents").exists())
                if target.is_symlink() or target.is_file():
                    target.unlink()
                else:
                    target.rmdir()

    def test_default_user_scope_and_explicit_project_scope(self):
        with mock.patch.object(Path, "home", return_value=self.home):
            result = SkillInstaller().install()
        self.assertEqual(result["scope"], "user")
        self.assertTrue((self.home / ".agents/skills/ssh-runner").is_symlink())
        with self.assertRaises(ToolError):
            SkillInstaller().install(scope="project")
        with self.assertRaises(ToolError):
            SkillInstaller().install(project_dir=self.home)

    def test_source_project_registration_keeps_canonical_directory(self):
        source = self.home / ".agents/skills/ssh-runner"
        shutil.copytree(self.source, source)
        with mock.patch(
            "ssh_runner.services.skills.skill_directory", return_value=source
        ):
            self.install(claude=True)
        self.assertFalse(source.is_symlink())
        self.assertEqual((self.home / ".claude/skills/ssh-runner").resolve(), source)
