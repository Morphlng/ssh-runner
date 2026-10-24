# 连接与诊断

```bash
ssh-runner --server dev doctor
ssh-runner --server dev connection status
ssh-runner --server dev connection close
```

config check 只校验本地；doctor 会连接远端，检查配置、OpenSSH、会话和工具，结果在 data.checks/data.capabilities。探测使用配置的 remote_python；缺依赖只指出受影响能力，不自动安装远端软件。

connection status/close 不新建网络连接。Windows 无共享 master，状态为 mode=direct、connected=false，close 无连接可关闭；POSIX 操作所选配置的本地主连接。状态成功不保证命令权限、目标路径可写或空间足够。

POSIX 默认 master 空闲 300 秒回收，无需每次调用后关闭。并发建连通过文件锁串行化，命令会话独立；并发量受服务器 MaxSessions/负载限制，业务资源冲突由调用方协调。

--no-reuse 本次使用短连接，不关闭已有 master。close 可能影响其他调用，仅在需要时使用。密钥身份改变后关闭旧连接或使用短连接。

连接池按认证、账号、路由、环境、实际 SSH 程序隔离；remote_python、job_root、sudo_password 不影响 SSH 身份。控制目录默认 /tmp/ssh-runner-UID，权限 0700，可用 --control-dir 覆盖；避免长路径、空格和 %。

Windows 状态目录默认 %LOCALAPPDATA%\ssh-runner\state，允许空格；私有文件和目录使用 Windows ACL。密码模式缺少 askpass.exe 时，在同一 Python 环境重新安装工具。Windows 独立连接允许并发：255 或超时记录后，后续请求冷却，已在途请求继续，成功请求不清除其他请求的失败记录。失败默认冷却 5 秒，不自动重试。认证被拒绝时检查所选模式、密钥/agent、账号与服务端策略，不猜测密码；OTP 等交互提示交由用户处理。普通命令 Permission denied 可能是文件权限问题。

分别排查 host key、DNS、网络、认证、远端权限。工具不关闭严格校验，不重放已提交命令，复用 socket 消失时不悄悄重新认证并执行同一命令。
