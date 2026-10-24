# 命令、脚本与结果

```bash
ssh-runner --server dev exec 'printf "%s\n" "$HOME"; pwd' --timeout 30
ssh-runner --server dev script ./check.sh --cwd /srv/project --arg 'two words' --arg='' --arg=--apply
ssh-runner --server dev exec 'python3 -' --stdin-file ./check.py
ssh-runner --server dev exec 'bash -s' --stdin-file ./check.sh
```

exec 接收一个完整命令字符串，由远端 shell 解释。本机用单引号保护远端 $、反引号和重定向；复杂命令优先写脚本。script 使用远端 sh -s --，参数按顺序字面传递。脚本路径或 --stdin-file 为 - 时读取调用方 stdin。

--cwd 为远端字面路径，不展开 ~；目录不存在则不继续执行。每次是独立 shell，无持续交互、PTY 或转发。

远端调用通用选项：--timeout 默认 30 秒；--control-dir 指定连接控制目录；--no-reuse 本次使用短连接。超时不保证远端进程终止，长任务优先用 job。

exec/script 支持 --max-output-bytes（每路 0–16777216，默认 65536）和 --stdout-file / --stderr-file。输出文件必须是新路径，权限 0600，无容量上限。

```bash
ssh-runner --server dev exec 'make test' --cwd /srv/project --timeout 600 \
  --max-output-bytes 16384 --stdout-file /tmp/test-unique.stdout --stderr-file /tmp/test-unique.stderr
```

输出持续排空。stdout_bytes/stderr_bytes 是原始流字节数，不是 JSON 大小；重定向工具 JSON 不能补回被截断的内容。文本对配置的 SSH/sudo 密码精确脱敏，其他秘密不一定识别，文件传输不做脱敏。

## sudo 与有限输入

`ssh-runner --server dev exec 'id' --sudo`：有 sudo_password 用 sudo -S，否则 sudo -n；不回退 SSH 密码，不自动重试。整个命令及 cwd 在 sudo shell 中运行，需服务器允许。目标 stdin 固定 /dev/null，不接受输入文件，也不能给脚本、传输或任务加 --sudo。

普通 y/n 提示优先用程序的非交互参数，或 --stdin-file 提供已知有限回答。不要向未知提示无条件发送 yes。要求 TTY 或 /dev/tty 时由用户使用交互 SSH。

## JSON 合同

默认输出一个结果：schema_version=1，operation 为 exec、file.upload、job.status 等。字段包括 exit_code、timed_out、error_kind、stdout/stderr、各路 _truncated/_bytes/_file、job_id、data。预期错误正文在 stderr，没有旧的顶层 error。

0 表示本次操作成功；2 常用于本地/参数错误，255 常用于 SSH 问题，但远端程序也可返回这些值。124 配合 timed_out 表示请求超时；130/143 表示本地中断，不能证明远端已停止。任务状态/传输摘要只在 data，stdout 不重复序列化。

参数错误也返回 JSON。--help/--version 是文本；未预期的程序错误保留 traceback。人工模式把 --format text 放在子命令前，仍有预览上限。等待超时和跟随 JSONL 见 [后台任务](jobs.md)。
