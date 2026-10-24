"""Private JSON configuration IO, independent of SSH execution."""

import dataclasses
import json
import os
import re
import stat
from pathlib import Path

from ..core.defaults import default_config_path
from ..core.errors import ConfigError
from ..resources import resource_text
from .models import (
    CONFIG_FIELDS,
    DEFAULT_FIELDS,
    MAX_CONFIG_BYTES,
    NAME_PATTERN,
    Config,
    ConfigCatalog,
)


def _read_private_file(path: Path) -> str:
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        raise ConfigError("无法打开配置文件；请确认文件存在且不是符号链接。") from None
    try:
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise ConfigError("配置必须是当前用户拥有的普通文件。")
            if info.st_mode & 0o077:
                raise ConfigError(
                    "配置文件权限过宽；请先执行 chmod 600 <config-file>。"
                )
            if info.st_size > MAX_CONFIG_BYTES:
                raise ConfigError("配置文件过大。")
            contents = handle.read(MAX_CONFIG_BYTES + 1)
    except OSError:
        raise ConfigError("无法读取配置文件。") from None
    if len(contents) > MAX_CONFIG_BYTES:
        raise ConfigError("配置文件过大。")
    try:
        return contents.decode("utf-8")
    except UnicodeError:
        raise ConfigError("配置文件必须使用 UTF-8 编码。") from None


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ConfigError("JSON 配置含重复字段。")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ConfigError("JSON 配置不支持 NaN 或 Infinity。")


def serialize_config(config: Config) -> str:
    """Serialize one resolved target as a private, versioned askpass snapshot."""
    values = dataclasses.asdict(config)
    values["env"] = dict(config.env)
    return (
        json.dumps(
            {
                "version": 1,
                "default_server": "snapshot",
                "servers": {"snapshot": values},
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )


class ConfigStore:
    def __init__(self, path=None):
        self.path = Path(path) if path is not None else default_config_path()

    def select(self, server=None):
        return self.load().select(server)

    def initialize(
        self,
    ) -> None:
        """Create a private template without replacing an existing file or symlink."""
        path = self.path
        try:
            template = resource_text("config.example.json").encode("utf-8")
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(
                str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
            )
            with os.fdopen(fd, "wb") as handle:
                handle.write(template)
        except FileExistsError:
            raise ConfigError(
                "配置已存在或路径为符号链接，未覆盖；请编辑现有配置或指定其他 --config 路径。"
            ) from None
        except OSError:
            raise ConfigError("无法创建配置模板；请检查路径和目录权限。") from None

    def load(
        self,
    ) -> ConfigCatalog:
        path = self.path
        try:
            data = json.loads(
                _read_private_file(path),
                object_pairs_hook=_unique_object,
                parse_constant=_invalid_constant,
            )
        except (ValueError, RecursionError):
            raise ConfigError("配置不是有效的 JSON；请检查语法。") from None
        if not isinstance(data, dict) or set(data) - {
            "version",
            "default_server",
            "defaults",
            "servers",
        }:
            raise ConfigError(
                "配置顶层必须是对象，且只能包含 version、default_server、defaults、servers。"
            )
        if type(data.get("version")) is not int or data["version"] != 1:
            raise ConfigError("配置 version 必须为 1。")
        defaults = data.get("defaults", {})
        if not isinstance(defaults, dict) or set(defaults) - DEFAULT_FIELDS:
            raise ConfigError(
                "defaults 字段不支持；目标地址、账号与密码需分别配置在服务中。"
            )
        if "env" in defaults and not isinstance(defaults["env"], dict):
            raise ConfigError("env 必须是 JSON 对象。")
        if "send_env" in defaults and not isinstance(defaults["send_env"], list):
            raise ConfigError("send_env 必须是 JSON 数组。")
        # Validate shared settings independently, even if every server overrides them.
        Config(
            "validation",
            "validation",
            defaults.get("port", 22),
            "",
            **{k: v for k, v in defaults.items() if k != "port"},
        )
        servers = data.get("servers")
        if not isinstance(servers, dict) or not 1 <= len(servers) <= 128:
            raise ConfigError("servers 必须是包含 1 到 128 个命名服务的对象。")
        configurations = []
        for name, profile in servers.items():
            if not re.fullmatch(NAME_PATTERN, name):
                raise ConfigError(
                    "服务名需为 1 到 64 个字母、数字、点、下划线或连字符。"
                )
            if not isinstance(profile, dict) or set(profile) - CONFIG_FIELDS:
                raise ConfigError("服务配置必须是对象，且不能包含未支持的字段。")
            if "host" not in profile or (
                profile.get("auth_mode", defaults.get("auth_mode")) != "key"
                and "password" not in profile
            ):
                raise ConfigError(
                    "Each server needs host; password modes also need a password field."
                )
            if "send_env" in profile and not isinstance(profile["send_env"], list):
                raise ConfigError("send_env 必须是 JSON 数组。")
            values = {**defaults, **profile}
            # Deep merge env only; arrays such as send_env replace the defaults.
            if "env" in defaults or "env" in profile:
                if not isinstance(profile.get("env", {}), dict):
                    raise ConfigError("env 必须是 JSON 对象。")
                values["env"] = {**defaults.get("env", {}), **profile.get("env", {})}
            configurations.append((name, Config(**values)))
        default = data.get("default_server")
        if "default_server" in data and (
            not isinstance(default, str) or default not in servers
        ):
            raise ConfigError("default_server 必须引用一个已配置的服务名。")
        return ConfigCatalog(tuple(configurations), default)
