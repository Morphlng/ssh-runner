# 后台任务与恢复

远端需要 Python 3.8+ 和 POSIX。先在本地生成并记录新的 32 位小写十六进制 UUID，再提交任务；回执丢失时查询原 ID，不生成新 ID 重试。

```bash
python3 -c 'import uuid; print(uuid.uuid4().hex)'
# 以下 ID 是示例，替换为本次生成的值。
ssh-runner --server dev job start 'make test' --cwd /srv/project \
  --job-id 0123456789abcdef0123456789abcdef --job-timeout 3600 --log-limit 10485760
ssh-runner --server dev job status 0123456789abcdef0123456789abcdef
ssh-runner --server dev job logs 0123456789abcdef0123456789abcdef --stream stdout --offset 0 --limit 16384
ssh-runner --server dev job list --offset 0 --limit 100
```

省略 ID 会自动生成并返回 job_id，但断线时可能拿不到回执。相同 ID 不会重新执行，即使命令变化也返回旧任务。整个生命周期使用同一服务、job_root 和 ID。

start/status/logs/cancel/list/delete 的外层 exit_code 是管理请求结果。状态看 data.state，任务退出码看 data.exit_code，不解析 stdout。queued/running 继续查询；finished 仍需检查退出码；cancelled/timed_out/failed 按停止或失败处理；unknown 是不确定状态，不能当作完成或据此重跑。

--timeout 默认 30 秒，限制单次管理请求。--job-timeout 默认 3600 秒、最多 7 天，限制新任务期限。每路日志默认保留前 10 MiB，--log-limit 范围 0–1 GiB，超出后继续排空，状态标记截断。

## 分页、等待与跟随

日志 offset/limit/next_offset 按原始字节计，单页 limit 最多 65536。为完整 UTF-8 字符可能多推进最多 3 字节，始终用返回的 next_offset。跨页脱敏；运行中可能暂缓返回密码前缀尾部。列表按 next_offset 续页，每次最多 4096 个 ID。

```bash
ssh-runner --server dev job wait 0123456789abcdef0123456789abcdef --wait-timeout 300
ssh-runner --server dev job follow 0123456789abcdef0123456789abcdef --stream stdout --offset 0
```

默认每秒查询，等待 300 秒；--poll-interval/--wait-timeout 可修改。到终态后外层返回任务退出码。等待到期返回 124、error_kind=wait_timeout、data.wait_timed_out=true、timed_out=false，不取消远端任务。unknown 或查询失败时停止等待，不重提。建连可能额外耗时。

follow 默认 JSONL：日志页 event=logs，最后 event=result。展示日志事件的 data.text，用 next_offset 恢复；最终结果可能保留末页，不再次展示。默认 stdout，stderr 需指定。

## 停止、删除、升级

```bash
ssh-runner --server dev job cancel 0123456789abcdef0123456789abcdef
ssh-runner --server dev job status 0123456789abcdef0123456789abcdef
ssh-runner --server dev job delete 0123456789abcdef0123456789abcdef
```

cancel 仅提交请求，随后查询到终态。worker 向自己的子进程组发送 TERM/KILL，不根据持久化旧 PID 发信号。自行脱离进程组或服务器清理需另行核实。

命令、状态、原始日志在远端 job_root/ID，默认 ~/.ssh-runner-jobs/ID。只允许删除已记录终态的任务；保留小型 tombstone 防止延迟请求重跑。不要把秘密写入任务命令。

升级客户端不替换运行中的 worker，仍读取原数据格式。job_root 改变后不迁移旧任务，查询旧任务需使用原目录；remote_python 变化只影响后续管理调用和新 worker。
