"""Immutable target configuration and named server catalog."""

import dataclasses
import ipaddress
import os
import re
from typing import Mapping, Optional, Tuple

from ..core.errors import ConfigError

MAX_CONFIG_BYTES = 65536
MAX_PASSWORD_BYTES = 1023  # OpenSSH's askpass buffer includes a terminator.
INTEGER_LIMITS = {
    "control_persist": (1, 86400),
    "server_alive_interval": (1, 3600),
    "server_alive_count_max": (1, 100),
    "connect_timeout": (1, 300),
    "reconnect_delay": (1, 300),
}
NAME_PATTERN = r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}"
ENV_PATTERN = r"[A-Za-z_][A-Za-z0-9_]{0,127}"


def _utf8_length(value: str) -> int:
    try:
        return len(value.encode("utf-8"))
    except UnicodeError:
        raise ConfigError("配置字符串必须是有效的 UTF-8 文本。") from None


def _environment_name(name) -> None:
    if not isinstance(name, str) or not re.fullmatch(ENV_PATTERN, name):
        raise ConfigError("环境变量名称必须是普通变量名，不支持通配符。")
    if name.startswith("SSH_") or name == "SSHPASS":
        raise ConfigError("不能透传 SSH 内部变量或认证凭据变量。")


def _environment_value(value) -> None:
    if (
        not isinstance(value, str)
        or any(c in value for c in "\x00\r\n")
        or _utf8_length(value) > 4096
    ):
        raise ConfigError("env 值必须是最多 4096 字节的单行字符串。")
    # SetEnv expansion differs between OpenSSH releases. Keep values literal.
    if "%" in value or "${" in value:
        raise ConfigError("env 值不支持 OpenSSH 的 % 或 ${...} 展开表达式。")


@dataclasses.dataclass(frozen=True)
class Config:
    host: str
    user: Optional[str] = None
    port: Optional[int] = None
    password: str = dataclasses.field(default="", repr=False)
    auth_mode: str = "password"
    password_prompt: str = ""
    reuse_connection: bool = True
    control_persist: int = 300
    server_alive_interval: int = 15
    server_alive_count_max: int = 3
    connect_timeout: int = 10
    reconnect_delay: int = 5
    env: Tuple[Tuple[str, str], ...] = dataclasses.field(
        default_factory=tuple, repr=False
    )
    send_env: Tuple[str, ...] = ()
    sudo_password: str = dataclasses.field(default="", repr=False)
    ssh_binary: str = "ssh"
    remote_python: str = "python3"
    job_root: str = "~/.ssh-runner-jobs"
    identity_file: str = ""
    identities_only: Optional[bool] = None
    certificate_file: str = ""
    known_hosts_file: str = ""
    ssh_config_file: str = ""

    @property
    def secrets(self) -> Tuple[str, ...]:
        return tuple(
            sorted(
                {self.password, self.sudo_password} - {""}, key=lambda s: (-len(s), s)
            )
        )

    def __post_init__(self) -> None:
        for name in (
            "identity_file",
            "certificate_file",
            "known_hosts_file",
            "ssh_config_file",
        ):
            value = getattr(self, name)
            if (
                not isinstance(value, str)
                or any(c in value for c in "\x00\r\n")
                or _utf8_length(value) > 4096
                or (value and ("%" in value or "${" in value))
            ):
                raise ConfigError(
                    name + " must be a single-line literal path (max 4096 bytes)."
                )
            if value:
                object.__setattr__(
                    self, name, os.path.abspath(os.path.expanduser(value))
                )
        if self.identities_only is not None and type(self.identities_only) is not bool:
            raise ConfigError("identities_only must be a boolean or null.")
        for name in ("ssh_binary", "remote_python", "job_root"):
            value = getattr(self, name)
            if (
                not isinstance(value, str)
                or not value
                or any(c in value for c in "\x00\r\n")
            ):
                raise ConfigError(name + " 必须是非空单行路径或程序名。")
        if not isinstance(self.host, str):
            raise ConfigError("host 格式不支持。")
        if ":" in self.host:
            try:
                ipaddress.IPv6Address(self.host)
            except ValueError:
                raise ConfigError("IPv6 地址格式不支持；请勿使用方括号。") from None
            if "%" in self.host:
                raise ConfigError("暂不支持带 scope ID 的 IPv6 地址。")
        elif not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", self.host):
            raise ConfigError("host 格式不支持。")
        if self.user is not None and (
            not isinstance(self.user, str)
            or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", self.user)
        ):
            raise ConfigError("user 格式不支持。")
        if self.port is not None and (
            type(self.port) is not int or not 1 <= self.port <= 65535
        ):
            raise ConfigError("port 必须是 1 到 65535 之间的整数。")
        if not isinstance(self.password, str) or any(
            c in self.password for c in "\x00\r\n"
        ):
            raise ConfigError("password 必须是单行字符串，且不能包含 NUL 字符。")
        if _utf8_length(self.password) > MAX_PASSWORD_BYTES:
            raise ConfigError("password 的 UTF-8 长度不能超过 1023 字节。")
        if not isinstance(self.sudo_password, str) or any(
            c in self.sudo_password for c in "\x00\r\n"
        ):
            raise ConfigError("sudo_password 必须是单行字符串，且不能包含 NUL 字符。")
        if _utf8_length(self.sudo_password) > MAX_PASSWORD_BYTES:
            raise ConfigError("sudo_password 的 UTF-8 长度不能超过 1023 字节。")
        if not isinstance(self.auth_mode, str) or self.auth_mode not in {
            "password",
            "keyboard-interactive",
            "key",
        }:
            raise ConfigError(
                "auth_mode 必须为 password、keyboard-interactive 或 key。"
            )
        if type(self.reuse_connection) is not bool:
            raise ConfigError("reuse_connection 必须为 JSON 布尔值。")
        for field, (minimum, maximum) in INTEGER_LIMITS.items():
            value = getattr(self, field)
            if type(value) is not int or not minimum <= value <= maximum:
                raise ConfigError(
                    "%s 必须是 %d 到 %d 之间的整数。" % (field, minimum, maximum)
                )
        if not isinstance(self.password_prompt, str):
            raise ConfigError("password_prompt 必须是字符串。")
        if any(c in self.password_prompt for c in "\x00\r\n"):
            raise ConfigError("password_prompt 必须是单行字符串。")
        object.__setattr__(self, "password_prompt", self.password_prompt.strip())
        if self.password_prompt:
            lowered = self.password_prompt.lower()
            forbidden = (
                "otp",
                "passphrase",
                "verification",
                "new password",
                "old password",
                "验证码",
                "一次性",
                "新密码",
                "旧密码",
            )
            standard_prompt = re.fullmatch(
                re.escape(self.user or "") + r"@[A-Za-z0-9_.:\[\]-]+'s password:",
                self.password_prompt,
            )
            if (
                self.auth_mode in {"key"}
                or any(c in self.password_prompt for c in "\x00\r\n")
                or _utf8_length(self.password_prompt) > 1023
                or not ("password" in lowered or "密码" in lowered)
                or (not standard_prompt and any(word in lowered for word in forbidden))
            ):
                raise ConfigError(
                    "password_prompt 仅支持普通登录密码提示，不支持 OTP 或密码变更提示。"
                )
        if isinstance(self.env, Mapping):
            pairs = tuple(self.env.items())
        elif isinstance(self.env, tuple) and all(
            isinstance(p, tuple) and len(p) == 2 for p in self.env
        ):
            pairs = self.env
        else:
            raise ConfigError("env 必须是变量名到字符串值的映射。")
        names = set()
        for name, value in pairs:
            _environment_name(name)
            _environment_value(value)
            if name in names:
                raise ConfigError("env 中的变量名重复。")
            names.add(name)
            if any(secret in value for secret in self.secrets):
                raise ConfigError("env 不能包含登录密码或 sudo 密码。")
        if not isinstance(self.send_env, (tuple, list)):
            raise ConfigError("send_env 必须是变量名数组。")
        for name in self.send_env:
            _environment_name(name)
        if len(set(self.send_env)) != len(self.send_env):
            raise ConfigError("send_env 中的变量名重复。")
        if (
            len(names | set(self.send_env)) > 64
            or sum(_utf8_length(v) for _, v in pairs) > 16384
        ):
            raise ConfigError(
                "每个服务最多配置 64 个环境变量，总值长度最多 16384 字节。"
            )
        object.__setattr__(self, "env", tuple(sorted(pairs)))
        object.__setattr__(self, "send_env", tuple(sorted(self.send_env)))

    def require_password(self) -> None:
        if self.auth_mode != "key" and not self.password:
            raise ConfigError("请在本机配置文件中填写所选服务的 password。")


CONFIG_FIELDS = frozenset(field.name for field in dataclasses.fields(Config))
DEFAULT_FIELDS = CONFIG_FIELDS - {"host", "user", "password", "sudo_password"}


def resolve_environment(
    config: Config, environment: Optional[Mapping[str, str]] = None
) -> Config:
    """Capture only named local variables; explicit env values take precedence."""
    config.require_password()
    environment = os.environ if environment is None else environment
    values = {}
    explicit = dict(config.env)
    for name in config.send_env:
        if name not in explicit:
            if name not in environment:
                raise ConfigError("所选服务要求透传的本地环境变量未设置。")
            values[name] = environment[name]
    values.update(explicit)
    return dataclasses.replace(config, env=values, send_env=())


@dataclasses.dataclass(frozen=True)
class ConfigCatalog:
    servers: Tuple[Tuple[str, Config], ...] = dataclasses.field(repr=False)
    default_server: Optional[str] = None

    def select(self, name: Optional[str] = None) -> Tuple[str, Config]:
        if name is not None and (
            not isinstance(name, str) or not re.fullmatch(NAME_PATTERN, name)
        ):
            raise ConfigError("服务名格式不支持。")
        selected = name if name is not None else self.default_server
        if selected is None:
            if len(self.servers) != 1:
                raise ConfigError(
                    "有多个服务且未设置 default_server；请用 --server 指定名称。"
                )
            selected = self.servers[0][0]
        for server_name, config in self.servers:
            if server_name == selected:
                return server_name, resolve_environment(config)
        raise ConfigError("未找到指定名称的 SSH 服务。")

    def summaries(self) -> list:
        default = self.default_server or (
            self.servers[0][0] if len(self.servers) == 1 else None
        )
        return [
            {
                "name": name,
                "default": name == default,
                "host": config.host,
                "user": config.user,
                "port": config.port,
                "auth_mode": config.auth_mode,
                "password_configured": bool(config.password),
                "sudo_password_configured": bool(config.sudo_password),
                "identity_file_configured": bool(config.identity_file),
                "certificate_file_configured": bool(config.certificate_file),
                "known_hosts_file_configured": bool(config.known_hosts_file),
                "ssh_config_file_configured": bool(config.ssh_config_file),
            }
            for name, config in self.servers
        ]
