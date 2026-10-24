"""Directory operations reuse exec, verified binary transfer and safe archives."""

import dataclasses
import os
import tarfile
import uuid
from pathlib import Path

from .._platform import archive_options, private_temporary_file
from ..core.errors import ToolError
from ..core.results import redact, with_data
from ..remote import archive
from ..resources import remote_source
from .execution import remote_path
from .protocol import python_command


class DirectoryTransfer:
    def __init__(self, executor, files, protocol):
        self.executor = executor
        self.files = files
        self.protocol = protocol

    def upload(self, local_path, destination, **kwargs):
        """Publish the contents of a real directory at a new remote destination."""
        config = self.executor.config
        target = remote_path(destination)
        with private_temporary_file() as snapshot:
            try:
                archive.pack_directory(local_path, snapshot, **archive_options("pack"))
                size, digest = archive.digest(snapshot)
            except (OSError, ValueError, tarfile.TarError) as error:
                raise ToolError(
                    "目录快照失败：" + redact(str(error), config.secrets)
                ) from error
            result, data = self.protocol.receive(
                python_command(
                    remote_source("archive"),
                    "receive",
                    target,
                    size,
                    digest,
                    interpreter=config.remote_python,
                ),
                stdin_file=snapshot,
                **kwargs,
            )
        if data is not None:
            return with_data(result, "directory.upload", redact(data, config.secrets))
        return dataclasses.replace(result, operation="directory.upload")

    def download(self, source, local_path, **kwargs):
        """Verify the entire archive before extracting and publishing a new local tree."""
        config = self.executor.config
        target = Path(local_path).absolute()
        if os.path.lexists(target):
            raise ToolError("目录下载目标已存在；目录传输不合并或覆盖现有目标。")
        marker = "SSH_RUNNER_HASH_" + uuid.uuid4().hex
        with private_temporary_file() as snapshot:
            result = self.files.receive_verified(
                python_command(
                    remote_source("archive"),
                    "send",
                    remote_path(source),
                    marker,
                    interpreter=config.remote_python,
                ),
                marker,
                snapshot,
                **kwargs,
            )
            if result.exit_code != 0:
                return dataclasses.replace(result, operation="directory.download")
            try:
                data = archive.unpack_directory(
                    snapshot, target, **archive_options("unpack")
                )
            except (OSError, ValueError, tarfile.TarError) as error:
                raise ToolError(
                    "目录解包或发布失败：" + redact(str(error), config.secrets)
                ) from error
            data.update(result.data)
        return with_data(result, "directory.download", redact(data, config.secrets))
