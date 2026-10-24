---
name: ssh-runner
description: 使用 ssh-runner 操作SSH 服务器，执行非交互命令和脚本、传输文件与目录、管理后台任务并诊断连接。用户要求使用 ssh_runner 或已有该工具配置时使用；不处理交互终端或端口转发。
---

# SSH Runner

通过本地 `ssh-runner` 调用系统 OpenSSH。支持 macOS/Linux/Windows 10/11 x64 客户端连接 Linux/macOS，Python 3.8+、OpenSSH 8.7+，无需第三方 Python 运行依赖。

## 定位工具与目标

优先使用已安装的 `ssh-runner`；同一 Python 环境可用 `python -m ssh_runner`。未安装且已有源码时，POSIX 可在仓库根目录使用模块入口；Windows 密码认证需先安装（开发用 editable install）以生成 askpass.exe。安装或升级时读 [安装说明](references/installation.md)，普通远端任务无需重新安装。

```bash
ssh-runner capabilities
ssh-runner config list
ssh-runner --server dev config check
```

以上操作不连接远端。根据 `data.servers` 选择用户所指服务，后续显式传 `--server`；目标有歧义时澄清。POSIX 默认配置为 `~/.config/ssh-runner/config.json`，Windows 为 `%LOCALAPPDATA%\ssh-runner\config.json`，自定义路径用 `--config`，不从当前目录查找。

**根参数 `--config`、`--server`、`--format` 必须放在子命令之前。** 具体能力以 capabilities 和当前 --help 为准。

配置缺失时用 config init 创建不覆盖的私有模板，由用户在本机填写凭据。不要读取或展示私有配置全文，不在聊天中索要密码。格式与认证见 [配置](references/configuration.md)。

## 选择操作

| 任务 | 命令 | 说明 |
| --- | --- | --- |
| 有限命令、管道或检查 | `exec 'COMMAND' --timeout 30` | [命令与输出](references/operations.md) |
| 多行 shell 脚本 | `script ./check.sh --arg VALUE` | [命令与输出](references/operations.md) |
| 文件或完整目录 | `file upload/download`、`directory upload/download` | [传输](references/transfers.md) |
| 长任务、可恢复进度 | `job start/status/logs/wait/follow/cancel/list/delete` | [后台任务](references/jobs.md) |
| 实际连接与依赖诊断 | `doctor` | [诊断](references/troubleshooting.md) |
| 主连接状态或关闭 | `connection status/close` | [诊断](references/troubleshooting.md) |

```bash
ssh-runner --server dev exec 'hostname; id; pwd' --timeout 30
ssh-runner --server dev exec 'tail -n 100 logs/app.log' --cwd /srv/project
ssh-runner --server dev script ./diagnose.sh --arg /srv/project --arg=--verbose
```

默认 JSON；人工阅读在子命令前加 --format text。每次执行使用独立 shell，连接复用不保留 cd 或变量。Windows 默认独立连接并允许并发；显式要求复用会报错，可用 --no-reuse。没有 PTY，普通命令 stdin 默认 /dev/null；OTP、密码变更、私钥口令和交互程序交由用户使用交互 SSH。

## 判断结果

- 检查 exit_code、timed_out、stderr、error_kind；非零退出时仍读取 JSON。
- 命令输出在 stdout/stderr；结构化操作结果只在 data，不再解析任务 stdout。schema_version 为 1。
- 每路预览默认 65536 字节，检查截断标志；完整日志需事先指定新的 --stdout-file / --stderr-file。
- job start 成功只代表管理请求成功，继续检查 data.state 和 data.exit_code。回执丢失时查询原 ID，不生成新 ID 重试。
- job follow 输出 JSONL：展示 event=logs 中的 data.text，以 event=result 判断最终状态，不重复展示末页。
- 超时或断线不证明远端已停止；重试前核实状态及产物，尤其是有副作用的命令。

sudo 使用 `exec 'COMMAND' --sudo`，密码只来自 sudo_password，不与脚本、输入文件、传输或任务组合。详见 [命令与输出](references/operations.md)。

遇到 OTP、验证码、口令或密码变更提示时停止自动认证，请用户通过交互 SSH 处理。不要把所有 255 或 Permission denied 都归因于密码。主机密钥需正常核对，不关闭严格校验。

按用户已授权的任务范围执行；技能不扩大授权。汇报实际目标、操作、结果和必要产物位置，未确认的结果保持未确认。
