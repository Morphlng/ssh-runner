"""Read-only, staged diagnostics using the same connection and execution paths."""

import dataclasses
import re
import shlex
import subprocess

from .._platform import WINDOWS, private_temporary_file
from ..config import resolve_environment
from ..config.checks import check_local_files
from ..core.errors import ToolError
from ..core.results import ExecutionResult, with_data
from ..ssh.auth import windows_askpass

PROBE = """printf 'SSH_RUNNER_DOCTOR_V1\\n'
for tool in sh cat wc tr mktemp chmod ln mv sha256sum shasum; do
  if command -v "$tool" >/dev/null 2>&1; then printf '%s=1\\n' "$tool"
  else printf '%s=0\\n' "$tool"; fi
done
if command -v __REMOTE_PYTHON__ >/dev/null 2>&1; then
  __REMOTE_PYTHON__ -c 'import sys,ctypes,os; print("python_supported=" + str(int(sys.version_info >= (3, 8) and os.name == "posix"))); c=ctypes.CDLL(None); print("atomic_rename=" + str(int((sys.platform == "darwin" and hasattr(c,"renamex_np")) or (sys.platform.startswith("linux") and hasattr(c,"renameat2")))))' 2>/dev/null || printf 'python_supported=0\\n'
fi
"""


def _failure_area(result):
    """Classify only explicit diagnostics from our fixed probe, not arbitrary commands."""
    text = result.stderr
    if result.timed_out:
        return "timeout"
    if (
        "Host key verification failed" in text
        or "REMOTE HOST IDENTIFICATION HAS CHANGED" in text
    ):
        return "host_key"
    if re.search(r"Permission denied \([a-z0-9,._@+-]+\)", text):
        return "authentication"
    if any(
        message in text
        for message in (
            "Could not resolve hostname",
            "Connection refused",
            "No route to host",
            "Network is unreachable",
            "Connection timed out",
        )
    ):
        return "network"
    return "unknown"


class Diagnostics:
    def __init__(self, executor):
        self.executor = executor

    def run(self, **kwargs):
        """Diagnose configuration, OpenSSH, session establishment and dependencies.

        The session stage includes route, trust and authentication; a remote exit code
        alone cannot reliably distinguish those causes. No command is retried.
        """
        config = self.executor.config
        checks = []
        try:
            config = resolve_environment(config)
            checks.append({"stage": "config", "status": "ok"})
        except ToolError as error:
            checks.append({"stage": "config", "status": "failed", "detail": str(error)})
            return with_data(
                ExecutionResult(2, False, "", ""),
                "doctor",
                {"checks": checks},
                error_kind="config",
            )
        stage = "openssh"
        try:
            if WINDOWS:
                stage = "local_permissions"
                check_local_files(config)
                checks.append({"stage": "local_permissions", "status": "ok"})
                stage = "private_storage"
                with private_temporary_file() as probe:
                    probe.write(b"local ACL probe")
                checks.append({"stage": "private_storage", "status": "ok"})
                if config.auth_mode != "key":
                    stage = "askpass"
                    windows_askpass()
                    checks.append({"stage": "askpass", "status": "ok"})
                stage = "openssh"
                version = self.executor.transport.process.run(
                    [self.executor.transport.binary, "-V"],
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=config.connect_timeout,
                )
                match = re.search(
                    r"OpenSSH(?:_for_Windows)?_(\d+)\.(\d+)",
                    (version.stdout or "") + (version.stderr or ""),
                )
                if (
                    version.returncode
                    or not match
                    or tuple(map(int, match.groups())) < (8, 7)
                ):
                    raise ToolError("Windows 需要原生 OpenSSH 8.7+。", kind="openssh")
                checks.append(
                    {
                        "stage": "connection_mode",
                        "status": "ok",
                        "mode": "direct",
                        "reuse_supported": False,
                    }
                )
            self.executor.transport.resolve_config(config)
            checks.append({"stage": "openssh", "status": "ok"})
        except (ToolError, OSError, subprocess.TimeoutExpired) as error:
            checks.append({"stage": stage, "status": "failed", "detail": str(error)})
            return with_data(
                ExecutionResult(2, False, "", ""),
                "doctor",
                {"checks": checks},
                error_kind=stage,
            )
        try:
            result = self.executor.execute(
                PROBE.replace("__REMOTE_PYTHON__", shlex.quote(config.remote_python)),
                **kwargs,
            )
        except ToolError as error:
            checks.append(
                {"stage": "ssh_session", "status": "failed", "detail": str(error)}
            )
            return with_data(
                ExecutionResult(2, False, "", ""),
                "doctor",
                {"checks": checks},
                error_kind="ssh_session",
            )
        if result.exit_code != 0:
            checks.append(
                {
                    "stage": "ssh_session",
                    "status": "failed",
                    "detail": result.stderr,
                    "failure_area": _failure_area(result),
                }
            )
            return with_data(
                result, "doctor", {"checks": checks}, error_kind="ssh_session"
            )
        lines = result.stdout.splitlines()
        if not lines or lines[0] != "SSH_RUNNER_DOCTOR_V1" or result.stdout_truncated:
            checks.append(
                {
                    "stage": "ssh_session",
                    "status": "failed",
                    "detail": "诊断响应不完整或含登录横幅。",
                }
            )
            return with_data(
                result, "doctor", {"checks": checks}, exit_code=2, error_kind="protocol"
            )
        checks.append({"stage": "ssh_session", "status": "ok"})
        found = dict(line.split("=", 1) for line in lines[1:] if "=" in line)
        present = lambda *names: all(found.get(name) == "1" for name in names)
        hashing = present("sha256sum") or present("shasum")
        capabilities = {
            "command": True,
            "script": present("sh"),
            "upload": present("sh", "cat", "wc", "tr", "mktemp", "chmod", "ln", "mv")
            and hashing,
            "download": present("sh", "cat", "wc", "tr") and hashing,
            "jobs": present("python_supported"),
            "directories": present("python_supported", "atomic_rename"),
        }
        missing = [name for name, available in capabilities.items() if not available]
        checks.append(
            {
                "stage": "remote_dependencies",
                "status": "warning" if missing else "ok",
                "unavailable_operations": missing,
            }
        )
        return with_data(
            dataclasses.replace(result, exit_code=1 if missing else 0),
            "doctor",
            {"checks": checks, "capabilities": capabilities},
            error_kind="remote_dependencies" if missing else None,
        )
