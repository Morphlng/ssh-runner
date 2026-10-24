# SSH Runner

给 AI Agent 用的非交互 SSH 工具。通过本机 OpenSSH 执行命令、脚本、文件与目录传输、后台任务；默认返回 JSON，无第三方 Python 运行依赖。

[English](README.md) · [认证配置](docs/authentication.md) · [Agent 指引](AGENT_GUIDE.md) · [贡献指南](CONTRIBUTING.md)

支持 macOS / Linux / Windows 10/11 x64 客户端、Python 3.8+、OpenSSH 8.7+，远端为 Linux/macOS。Windows 可原生运行，默认使用并发独立连接；安装、ACL 和行为差异见 [Windows 说明](docs/windows.md)。

## 安装与开始使用

公开首版正在准备中，可以从当前源码目录安装：

```bash
pipx install .
# 或 uv tool install .，或在虚拟环境中 python -m pip install .
ssh-runner capabilities
ssh-runner config init
```

发布到 PyPI 后可使用 `pipx install ssh-runner`。完整安装与升级方法见 [INSTALL.md](INSTALL.md)。

先用普通 SSH 核对服务器指纹，使用加密密钥时先通过 ssh-agent 解锁。若 `~/.ssh/config` 已有 `Host aliyun`，在本机编辑 `~/.config/ssh-runner/config.json`：

```json
{
  "version": 1,
  "default_server": "aliyun",
  "servers": {
    "aliyun": {"host": "aliyun", "auth_mode": "key"}
  }
}
```

```bash
chmod 600 ~/.config/ssh-runner/config.json
ssh-runner --server aliyun config check
ssh-runner --server aliyun exec 'hostname; id; pwd' --timeout 30
```

`host` 原样传给 OpenSSH，支持 SSH 别名及 `HostName`、`IdentityFile`、`ProxyJump` 等配置。省略 `user`、`port` 会继承 SSH 配置；显式填写则覆盖。`.ssh_config` 不是 OpenSSH 默认文件名：需要在 `~/.ssh/config` 中 Include，或在服务配置中设置 `"ssh_config_file": "~/.ssh_config"`。自定义文件通过 `ssh -F` 使用，遵循 OpenSSH 的 Include 与系统配置规则。

项目目录中的 `config.json` 不自动加载，请显式指定：

```bash
cp -n ./config.json.example ./config.json
# 在本机编辑 ./config.json，调整为下方命令使用的服务名。
chmod 600 ./config.json
ssh-runner --config ./config.json --server aliyun config check
ssh-runner --config ./config.json --server aliyun exec 'hostname'
```

根目录的 [config.json.example](config.json.example) 与 `config init` 模板一致，请在本机调整服务名及配置；`cp -n` 不覆盖已有文件，私有 `config.json` 已加入 Git 忽略规则。

WSL 使用自己的 home、SSH 配置、ssh-agent 和 known_hosts；请在 Linux 环境中准备这些文件与权限。Windows 路径不能直接当作 Linux 密钥路径使用。

## 认证

- 推荐 `auth_mode: key`，支持默认密钥和 ssh-agent。
- `identity_file` 显式指定私钥；此时默认启用 `identities_only`。未指定私钥时沿用 OpenSSH 配置。
- `certificate_file`、`known_hosts_file` 可分别指定 SSH 证书和主机信任文件。
- 路径支持 `~`；相对路径相对于调用进程的当前目录，不相对于 JSON 文件。
- `password`、`keyboard-interactive` 保留兼容；密码明文存储，使用者自行负责。为兼容原有配置，省略 `auth_mode` 仍为 password；请始终显式填写模式。模板优先展示 key。
- 仅自动回答允许的普通密码提示，拒绝 OTP、新旧密码、私钥口令等交互。详见 [认证说明](docs/authentication.md)。

`config check` 只检查所选服务及显式指定的本地文件，不调用 SSH，也不检查别名展开、SSH 配置中引用的文件、远端认证与信任；实际诊断用 `doctor`。

## 常用操作

全局参数 `--config`、`--server`、`--format` 必须放在子命令之前。

```bash
ssh-runner --server dev exec 'tail -n 100 app.log' --cwd /srv/project
ssh-runner --server dev script ./check.sh --arg='two words'
ssh-runner --server dev file upload ./patch.py /srv/project/patch.py --mode 644
ssh-runner --server dev directory download /srv/results ./results
ssh-runner --server dev job start 'make test' --job-id 0123456789abcdef0123456789abcdef
ssh-runner --server dev job follow 0123456789abcdef0123456789abcdef
ssh-runner --server dev doctor
ssh-runner --server dev connection close
ssh-runner skill install --claude
```

默认 JSON，`schema_version=1`；检查 `exit_code`、`timed_out`、`error_kind` 与截断标志。每条输出流默认预览 65536 字节，结构化结果在 `data`，远端输出在 `stdout/stderr`。`job follow` 输出 JSONL。后台任务启动成功只代表请求成功，仍需查询任务状态与最终退出码；回执丢失时查询原 ID。

macOS/Linux 通过 ControlMaster 跨进程复用，Windows 默认独立连接；失败不自动重试，命令不自动重放。超时与断线不保证远端停止，重试前核实状态和产物。每次调用使用独立 shell，不保留 cd/export；无 PTY、交互终端或端口转发。目录传输与任务要求远端 POSIX Python 3.8+。更详细的操作及 Python API 见 [英文 README](README.md) 和 [Agent 指引](AGENT_GUIDE.md)。

本工具不是沙箱，远端账号可执行的操作都可能被 Agent 执行。请使用专用低权限账号、受限 sudoers 和可信 SSH 配置。同 UID 的 Agent 仍可读取配置、密钥和 agent socket；输出脱敏只覆盖配置的 SSH/sudo 密码，文件内容不脱敏。参见 [SECURITY.md](SECURITY.md)。

MIT 许可证，见 [LICENSE](LICENSE)。
