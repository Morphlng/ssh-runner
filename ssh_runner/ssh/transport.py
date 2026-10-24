"""OpenSSH arguments and subprocess lifecycle, independent of connection pooling."""

import dataclasses
import os
import shutil
import subprocess
from contextlib import ExitStack, nullcontext
from pathlib import Path
from typing import List, Optional, Tuple

from .._platform import WINDOWS, open_private, private_temporary_file
from ..config import Config
from ..core.defaults import DEFAULT_OUTPUT_BYTES
from ..core.errors import ToolError
from ..core.results import ExecutionResult
from .auth import askpass_environment, get_policy
from .process import Capture, ProcessRunner, _as_text

BASE_OPTIONS = (
    "NumberOfPasswordPrompts=1",
    "StrictHostKeyChecking=yes",
    "UpdateHostKeys=no",
    "ClearAllForwardings=yes",
    "ForwardAgent=no",
    "ForwardX11=no",
    "Tunnel=no",
    "PermitLocalCommand=no",
    "RemoteCommand=none",
    "ConnectionAttempts=1",
    "LogLevel=ERROR",
)


def clean_environment():
    environment = os.environ.copy()
    for name in (
        "SSH_PASSWORD",
        "SSHPASS",
        "SSH_ASKPASS",
        "SSH_RUNNER_CONFIG_FILE",
        "SSH_RUNNER_ENV_FILE",
        "SSH_RUNNER_PROMPT_HOST",
    ):
        environment.pop(name, None)
    environment["SSH_ASKPASS_REQUIRE"] = "never"
    return environment


def prompt_host(config: Config, effective_settings: str) -> str:
    settings = dict(
        line.split(" ", 1) for line in effective_settings.splitlines() if " " in line
    )
    alias = settings.get("hostkeyalias")
    return alias if alias and alias != "none" else settings.get("hostname", config.host)


def _result(
    config: Config, code: int, timed_out: bool, stdout: str, stderr: str
) -> ExecutionResult:
    code = 124 if timed_out else (128 - code if code < 0 else code)
    for secret in config.secrets:
        stdout = stdout.replace(secret, "[REDACTED]")
        stderr = stderr.replace(secret, "[REDACTED]")
    return ExecutionResult(code, timed_out, stdout, stderr)


class OpenSSHTransport:
    def __init__(self, ssh_binary="ssh", process_runner=None):
        found = shutil.which(ssh_binary)
        if found is None:
            raise ToolError("找不到 OpenSSH 可执行程序：" + ssh_binary, kind="openssh")
        self.binary = str(Path(found).resolve())
        self.process = process_runner or ProcessRunner()

    def arguments(
        self,
        config: Config,
        command: Optional[str] = None,
        *,
        control_path: Optional[Path] = None,
        operation: str = "command",
    ) -> List[str]:
        """Separate master bootstrap from command clients; forbid mux fallback."""
        if operation not in {"command", "master", "inspect", "check", "exit"}:
            raise ValueError("unsupported SSH operation")
        if WINDOWS and (
            control_path is not None or operation in {"master", "check", "exit"}
        ):
            raise ToolError(
                "Windows OpenSSH 不支持连接复用；请设置 reuse_connection=false 或使用 --no-reuse。"
            )
        master = operation == "master"
        mux_only = control_path is not None and not master
        options = (
            list(BASE_OPTIONS)
            + get_policy(config.auth_mode).options(mux_only)
            + [
                "ControlMaster=%s" % ("yes" if master else "no"),
                "ControlPath=%s"
                % (str(control_path) if control_path is not None else "none"),
                "ControlPersist=%s" % (config.control_persist if master else "no"),
                "SessionType=%s" % ("none" if master else "default"),
                "ForkAfterAuthentication=%s" % ("yes" if master else "no"),
                "ConnectTimeout=%s" % config.connect_timeout,
                "ServerAliveInterval=%s" % config.server_alive_interval,
                "ServerAliveCountMax=%s" % config.server_alive_count_max,
            ]
        )
        if mux_only:
            # OpenSSH normally opens a new network connection if the control socket
            # disappears. A local false proxy makes that fallback fail locally.
            options.extend(["ProxyCommand=false", "ProxyJump=none"])
        if config.env:
            # One SetEnv directive: older OpenSSH also treats the first occurrence
            # as authoritative. Values never modify the local process environment.
            assignments = []
            for name, value in config.env:
                literal = (name + "=" + value).replace("\\", "\\\\").replace('"', '\\"')
                assignments.append('"' + literal + '"')
            options.append("SetEnv=" + " ".join(assignments))
            # Mux masters permit client env requests using their SendEnv names.
            options.append("SendEnv=" + " ".join(name for name, _ in config.env))
        args = [self.binary, "-T"]
        if config.ssh_config_file:
            args.extend(["-F", config.ssh_config_file])
        if config.port is not None:
            args.extend(["-p", str(config.port)])
        if config.user is not None:
            args.extend(["-l", config.user])
        if config.identity_file:
            args.extend(["-i", config.identity_file])
        identities_only = config.identities_only
        if identities_only is None and config.identity_file:
            identities_only = True
        if identities_only is not None:
            options.append("IdentitiesOnly=" + ("yes" if identities_only else "no"))
        for field, option in (
            ("certificate_file", "CertificateFile"),
            ("known_hosts_file", "UserKnownHostsFile"),
        ):
            value = getattr(config, field)
            if value:
                # -o uses ssh_config syntax even though argv bypasses the shell.
                literal = value.replace("\\", "\\\\").replace('"', '\\"')
                options.append(option + '="' + literal + '"')
        if operation == "inspect":
            args.append("-G")
        elif operation in {"check", "exit"}:
            args.extend(["-O", operation])
        for option in options:
            args.extend(["-o", option])
        args.append(config.host)
        if command is not None:
            args.append(command)
        return args

    def resolve_config(self, config: Config) -> Tuple[Config, str]:
        """Resolve Host aliases and fingerprint effective route/trust settings."""
        try:
            evaluated = self.process.run(
                self.arguments(config, operation="inspect"),
                env=clean_environment(),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=config.connect_timeout,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise ToolError("无法读取 OpenSSH 的有效配置。") from None
        if evaluated.returncode != 0:
            raise ToolError("OpenSSH 配置校验失败；请检查本机配置与 OpenSSH 版本。")
        settings = dict(
            line.split(" ", 1) for line in evaluated.stdout.splitlines() if " " in line
        )
        if config.user is None or config.port is None:
            try:
                config = dataclasses.replace(
                    config,
                    user=config.user if config.user is not None else settings["user"],
                    port=(
                        config.port
                        if config.port is not None
                        else int(settings["port"])
                    ),
                )
            except (KeyError, ValueError):
                raise ToolError(
                    "OpenSSH did not resolve the target user and port."
                ) from None
        if config.auth_mode == "password" and not config.password_prompt:
            host = prompt_host(config, evaluated.stdout)
            config = dataclasses.replace(
                config, password_prompt="%s@%s's password:" % (config.user, host)
            )
        return config, evaluated.stdout

    def run_command(
        self,
        config: Config,
        command: str,
        timeout: float,
        control_path: Optional[Path] = None,
        *,
        prompt_host: Optional[str] = None,
        stdin=None,
        max_output_bytes: int = DEFAULT_OUTPUT_BYTES,
        stdout_file=None,
        stderr_file=None,
        binary_stdout: bool = False,
    ) -> ExecutionResult:
        context = (
            askpass_environment(config, prompt_host)
            if control_path is None and config.auth_mode != "key"
            else nullcontext(clean_environment())
        )
        try:
            with ExitStack() as stack:
                environment = stack.enter_context(context)

                def output(target):
                    if target is None or hasattr(target, "write"):
                        return target
                    # Never truncate existing files or follow symlinks. Logs are private.
                    fd = open_private(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
                    return stack.enter_context(os.fdopen(fd, "wb"))

                out_file, err_file = output(stdout_file), output(stderr_file)
                secret = tuple(value.encode("utf-8") for value in config.secrets)
                out = Capture(max_output_bytes, secret, out_file, binary_stdout)
                err = Capture(max_output_bytes, secret, err_file)
                arguments = self.arguments(config, command, control_path=control_path)
                if stdin is None:
                    # Explicitly tell OpenSSH there is no command input. Win32
                    # console/NUL handle behavior must not leave the channel open.
                    arguments.insert(1, "-n")
                process = self.process.spawn(
                    arguments,
                    env=environment,
                    stdin=stdin if stdin is not None else subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    start_new_session=True,
                )
                timed_out = self.process.drain(process, out, err, timeout)
        except OSError:
            raise ToolError(
                "本地 SSH 或输入输出文件操作失败；输出文件必须不存在，且目录需可写。"
            ) from None
        code = process.returncode
        result = ExecutionResult(
            124 if timed_out else (128 - code if code < 0 else code),
            timed_out,
            out.text,
            err.text,
        )
        return dataclasses.replace(
            result,
            stdout_truncated=out.truncated,
            stderr_truncated=err.truncated,
            stdout_bytes=out.total,
            stderr_bytes=err.total,
            stdout_file=(
                str(stdout_file) if isinstance(stdout_file, (str, Path)) else None
            ),
            stderr_file=(
                str(stderr_file) if isinstance(stderr_file, (str, Path)) else None
            ),
        )

    def start_master(
        self, config: Config, control_path: Path, *, prompt_host: Optional[str] = None
    ) -> ExecutionResult:
        """Wait for the bootstrap parent, never for background master's output EOF."""
        try:
            with (
                askpass_environment(config, prompt_host)
                if config.auth_mode != "key"
                else nullcontext(clean_environment())
            ) as environment, private_temporary_file() as diagnostics:
                process = self.process.spawn(
                    self.arguments(
                        config, control_path=control_path, operation="master"
                    ),
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=diagnostics,
                    start_new_session=True,
                )
                timed_out = False
                try:
                    process.wait(timeout=config.connect_timeout)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    self.process.stop(process)
                except BaseException:
                    self.process.stop(process)
                    raise
                diagnostics.seek(0)
                # Master uses LogLevel=ERROR. Only bounded startup diagnostics are
                # returned; this unlinked file is never a persistent execution log.
                stderr = _as_text(diagnostics.read(65536))
                result = _result(config, process.returncode, timed_out, "", stderr)
                result = get_policy(config.auth_mode).diagnose_bootstrap(result)
                return result
        except OSError:
            raise ToolError("无法建立 SSH 主连接；请检查程序与临时目录权限。") from None

    def control_command(
        self, config: Config, control_path: Path, operation: str
    ) -> bool:
        """These operations only contact the local socket, never the remote host."""
        try:
            checked = self.process.run(
                self.arguments(config, control_path=control_path, operation=operation),
                env=clean_environment(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=min(config.connect_timeout, 3),
            )
        except subprocess.TimeoutExpired:
            raise ToolError(
                "SSH 控制进程无响应；本次操作未尝试新建网络连接。"
            ) from None
        except OSError:
            raise ToolError("无法操作本地 SSH 控制连接。") from None
        return checked.returncode == 0
