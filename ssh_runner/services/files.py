"""Atomic single-file transfers over exec channels (no SFTP dependency)."""

import dataclasses
import os
import re
import shlex
import stat
import tempfile
import uuid
from pathlib import Path

from ..core.errors import ToolError
from ..core.results import with_data
from ..remote.archive import digest as _digest
from .execution import remote_path


class FileTransfer:
    def __init__(self, executor):
        self.executor = executor

    def receive_verified(self, command, marker, output, **kwargs):
        """Shared binary receiver for files and directory archives; never publish here."""
        kwargs["max_output_bytes"] = max(4096, kwargs.get("max_output_bytes", 65536))
        result = self.executor.execute(
            command, stdout_file=output, _binary_stdout=True, **kwargs
        )
        if result.exit_code != 0:
            return result
        size, digest = _digest(output)
        pattern = r"(?m)^" + re.escape(marker) + r" ([0-9]+) ([a-f0-9]{64})\r?$"
        matches = list(re.finditer(pattern, result.stderr))
        if (
            len(matches) != 1
            or int(matches[0].group(1)) != size
            or matches[0].group(2) != digest
        ):
            raise ToolError(
                "下载大小或 SHA-256 校验失败；可能存在登录横幅或文件在传输中变化，未覆盖目标。"
            )
        return with_data(
            result,
            "file.download",
            {"bytes": size, "sha256": digest},
            stderr=re.sub(pattern + r"\n?", "", result.stderr),
        )

    def upload(self, local_path, destination, *, overwrite=False, mode="600", **kwargs):
        """Verify size and SHA-256 before publishing a remote sibling temporary file."""
        if mode not in {"600", "640", "644", "700", "750", "755"}:
            raise ToolError("上传文件 mode 仅支持 600/640/644/700/750/755。")
        target = shlex.quote(remote_path(destination))
        with tempfile.TemporaryFile() as snapshot:
            try:
                with os.fdopen(
                    os.open(str(local_path), os.O_RDONLY | os.O_NONBLOCK), "rb"
                ) as source:
                    if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                        raise ToolError("上传源必须是普通文件。")
                    for chunk in iter(lambda: source.read(65536), b""):
                        snapshot.write(chunk)
                snapshot.seek(0)
                size, digest = _digest(snapshot)
            except OSError as error:
                raise ToolError("无法读取上传源或创建本地快照。") from error
            publish = (
                'mv -f "$tmp" "$dest"'
                if overwrite
                else 'ln "$tmp" "$dest" || exit 1\nrm -f "$tmp"'
            )
            script = """set -eu
umask 077
dest=%s
[ ! -d "$dest" ] || { echo 'destination is a directory' >&2; exit 2; }
if command -v sha256sum >/dev/null 2>&1; then hash=sha256sum
elif command -v shasum >/dev/null 2>&1; then hash='shasum -a 256'
else echo 'sha256sum or shasum required' >&2; exit 2; fi
tmp=$(mktemp "${dest}.ssh-runner.XXXXXX")
trap 'rm -f "$tmp"' EXIT
trap 'exit 1' HUP INT TERM
cat > "$tmp"
[ "$(wc -c < "$tmp" | tr -d ' ')" = '%s' ] || { echo 'size mismatch' >&2; exit 1; }
actual=$($hash < "$tmp")
[ "${actual%%%% *}" = '%s' ] || { echo 'sha256 mismatch' >&2; exit 1; }
chmod %s "$tmp"
%s
printf '%%s\\n' '%s'
""" % (
                target,
                size,
                digest,
                mode,
                publish,
                digest,
            )
            result = self.executor.execute(
                "sh -c " + shlex.quote(script), stdin_file=snapshot, **kwargs
            )
            if result.exit_code == 0:
                return with_data(
                    result,
                    "file.upload",
                    {"bytes": size, "sha256": digest},
                )
            return dataclasses.replace(result, operation="file.upload")

    def download(self, source, local_path, *, overwrite=False, **kwargs):
        """Receive binary bytes into a private temp file, publish only on SSH success."""
        target = Path(local_path).absolute()
        temporary = None
        try:
            if target.exists() and not overwrite:
                raise ToolError("下载目标已存在；覆盖需要 overwrite。")
            try:
                output = tempfile.NamedTemporaryFile(
                    prefix=".ssh-download-", dir=target.parent, delete=False
                )
            except OSError as error:
                raise ToolError("无法创建本地下载临时文件。") from error
            with output:
                temporary = Path(output.name)
                marker = "SSH_RUNNER_HASH_" + uuid.uuid4().hex
                script = """set -eu
    src=%s
    [ -f "$src" ] || { echo 'source must be a regular file' >&2; exit 2; }
    if command -v sha256sum >/dev/null 2>&1; then hash=sha256sum
    elif command -v shasum >/dev/null 2>&1; then hash='shasum -a 256'
    else echo 'sha256sum or shasum required' >&2; exit 2; fi
    actual=$($hash < "$src")
    size=$(wc -c < "$src" | tr -d ' ')
    printf '%%s %%s %%s\\n' '%s' "$size" "${actual%%%% *}" >&2
    cat "$src"
    """ % (
                    shlex.quote(remote_path(source)),
                    marker,
                )
                result = self.receive_verified(
                    "sh -c " + shlex.quote(script), marker, output, **kwargs
                )
            if result.exit_code != 0:
                return result
            try:
                if overwrite:
                    os.replace(temporary, target)
                else:
                    os.link(
                        temporary, target
                    )  # Atomic no-clobber, including dangling symlinks.
                    temporary.unlink()
            except OSError as error:
                raise ToolError("下载文件提交失败；原目标未被部分下载覆盖。") from error
            temporary = None
            return dataclasses.replace(
                result,
                stdout_file=str(target),
            )
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
