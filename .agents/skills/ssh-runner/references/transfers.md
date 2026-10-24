# 文件与目录传输

```bash
ssh-runner --server dev file upload ./patch.py /srv/project/patch.py --mode 644
ssh-runner --server dev file download /tmp/build.log ./build.log
ssh-runner --server dev directory upload ./build /srv/releases/build-new
ssh-runner --server dev directory download /srv/reports ./reports-new
```

upload 是本地到远端，download 相反。路径按字面处理，不展开 ~ 或通配符；目标父目录必须存在。

单文件默认不覆盖，任务明确需要替换时用 --overwrite。上传 mode 默认 600，可选 640/644/700/750/755。验证大小与 SHA-256 后才提交目标，失败不发布部分内容。摘要在 data.bytes/data.sha256，下载 stdout_file 为本地路径。原始二进制内容不脱敏。

Windows 本地路径用原生格式，远端仍用 POSIX 路径；本地 shell 脚本保存为 LF，工具不改写脚本字节。Windows 目录上传文件默认 0644、目录 0755；下载不模拟 POSIX 权限。归档拒绝 reparse point、Windows 设备名/ADS/路径逃逸与大小写冲突。无需 SFTP。远端需 POSIX sh、cat、wc、tr、sha256sum 或 shasum；上传另需 mktemp、chmod、ln/mv。可用 --max-output-bytes 限制诊断预览。

目录目标必须不存在，包括空目录和符号链接；不接受 --overwrite，不合并、不增量同步。保留普通文件、空目录、权限和修改时间；拒绝符号链接、特殊文件和危险归档路径。远端需 Python 3.8+，接收端需 macOS/Linux 原子不覆盖重命名。

先制作快照、校验完整归档，再解包和原子发布。需足够临时磁盘空间；doctor 只检查工具依赖，不保证具体路径权限和剩余空间。
