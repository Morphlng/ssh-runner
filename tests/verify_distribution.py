"""Validate built wheel/sdist and pip Git installs outside the checkout.

Run explicitly after `python -m build --outdir DIST`:
    python tests/verify_distribution.py DIST
Uses temporary venvs, HOME directories, and a local Git fixture. Build dependencies
may be downloaded by pip; no business SSH server or user configuration is used.
"""

import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path


def check_archive_members(names):
    """Private local settings and repository metadata must never be distributed."""
    forbidden = {
        "config.json",
        ".ssh_config",
        "ssh_config",
        "OPEN_SOURCE_PLAN.md",
        "CHANGELOG.md",
        ".git",
        ".claude",
    }
    for name in names:
        parts = Path(name).parts
        assert not forbidden.intersection(parts), name
        assert not any(part.startswith(".validation") for part in parts), name
        assert not name.endswith((".pem", ".key")), name
    assert any(
        name.endswith("ssh_runner/resources/config.example.json") for name in names
    )


def run(arguments, *, cwd, env, expected=0):
    completed = subprocess.run(
        [str(arg) for arg in arguments],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    if completed.returncode != expected:
        raise AssertionError(
            "%s\n%s\n%s" % (arguments, completed.stdout, completed.stderr)
        )
    return completed.stdout


def validate(root, name, requirement):
    home = root / (name + "-home")
    home.mkdir()
    env = dict(
        os.environ,
        HOME=str(home),
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
    )
    env.pop("PYTHONPATH", None)
    venv = root / (name + "-venv")
    run([sys.executable, "-m", "venv", venv], cwd=home, env=env)
    python = venv / "bin/python"
    cli = venv / "bin/ssh-runner"
    run(
        [python, "-m", "pip", "--isolated", "install", "--no-deps", requirement],
        cwd=home,
        env=env,
    )
    capabilities = json.loads(run([cli, "capabilities"], cwd=home, env=env))
    installed_version = run(
        [python, "-c", "from ssh_runner import __version__; print(__version__)"],
        cwd=home,
        env=env,
    ).strip()
    assert capabilities["data"]["version"] == installed_version
    assert capabilities == json.loads(
        run([python, "-m", "ssh_runner", "capabilities"], cwd=home, env=env)
    )
    run([cli, "config", "init"], cwd=home, env=env)
    example = Path(__file__).resolve().parents[1] / "config.json.example"
    generated = home / ".config/ssh-runner/config.json"
    assert json.loads(generated.read_text()) == json.loads(example.read_text())
    run([cli, "config", "init"], cwd=home, env=env, expected=2)
    assert (home / ".config/ssh-runner/config.json").stat().st_mode & 0o777 == 0o600
    assert (
        len(
            json.loads(run([cli, "config", "list"], cwd=home, env=env))["data"][
                "servers"
            ]
        )
        == 2
    )
    run([cli, "skill", "install", "--claude"], cwd=home, env=env)
    link = home / ".agents/skills/ssh-runner"
    inode = link.lstat().st_ino
    run([cli, "skill", "install", "--claude"], cwd=home, env=env)
    assert link.lstat().st_ino == inode
    source = link.resolve()
    assert venv in source.parents, source
    assert (home / ".claude/skills/ssh-runner").resolve() == source
    for document in source.rglob("*.md"):
        for reference in re.findall(r"\]\(([^)#]+)", document.read_text()):
            if "://" not in reference:
                assert (document.parent / reference).is_file(), (document, reference)
    code = r"""
import json, os, subprocess, sys
from pathlib import Path
from importlib.metadata import metadata
from ssh_runner import Config
from ssh_runner.resources import remote_source
from ssh_runner.ssh.auth import askpass_environment
import ssh_runner
assert set(p.name for p in Path(ssh_runner.__file__).parent.glob("*.py")) == {"__init__.py", "__main__.py"}
assert all("extra ==" in item for item in metadata("ssh-runner").get_all("Requires-Dist", []))
config = Config("fixture", "user", 22, "fixture-password")
with askpass_environment(config) as environment:
    answer = subprocess.run([environment["SSH_ASKPASS"], "user@fixture's password:"], env=environment, capture_output=True, text=True)
    assert answer.returncode == 0, answer.stderr
    assert answer.stdout == "fixture-password\n"
source = remote_source("jobs")
response = subprocess.run([sys.executable, "-c", source, str(Path.home() / "job root")],
    input=json.dumps({"operation": "list", "offset": 0, "limit": 10}), capture_output=True, text=True)
assert response.returncode == 0, response.stderr
assert json.loads(response.stdout)["job_ids"] == []
compile(remote_source("archive"), "archive.py", "exec")
"""
    run([python, "-c", code], cwd=home, env=env)
    print(
        name + ": installed CLI, askpass, helpers, template and skill verified",
        flush=True,
    )


def main():
    dist = Path(sys.argv[1]).resolve()
    wheel = next(dist.glob("*.whl"))
    sdist = next(dist.glob("*.tar.gz"))
    with zipfile.ZipFile(wheel) as archive:
        check_archive_members(archive.namelist())
    with tarfile.open(sdist) as archive:
        check_archive_members(archive.getnames())
        assert any(name.endswith("/config.json.example") for name in archive.getnames())
    with tempfile.TemporaryDirectory(prefix="ssh-distribution-") as directory:
        root = Path(directory).resolve()
        validate(root, "wheel", str(wheel))
        validate(root, "sdist", str(sdist))
        checkout = root / "checkout"
        checkout.mkdir()
        with tarfile.open(sdist) as archive:
            # This is our just-built artifact, not an untrusted downloaded archive.
            archive.extractall(checkout)
        source = next(checkout.iterdir())
        env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
        run(["git", "init", "-b", "fixture"], cwd=source, env=env)
        run(["git", "add", "."], cwd=source, env=env)
        run(
            [
                "git",
                "-c",
                "user.name=Distribution Test",
                "-c",
                "user.email=test@example.invalid",
                "-c",
                "commit.gpgsign=false",
                "commit",
                "-m",
                "fixture",
            ],
            cwd=source,
            env=env,
        )
        validate(root, "git", "git+" + source.as_uri() + "@fixture")


if __name__ == "__main__":
    main()
