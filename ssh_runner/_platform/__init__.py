"""Local OS primitives. Remote helpers must remain independent of this package."""

import os
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path

WINDOWS = os.name == "nt"

if WINDOWS:
    from .windows import (
        directory_link,
        lock_file,
        open_private,
        open_regular,
        private_directory,
        publish_file,
        unlock_file,
    )
else:
    from .posix import (
        directory_link,
        lock_file,
        open_private,
        open_regular,
        private_directory,
        publish_file,
        unlock_file,
    )


@contextmanager
def private_temporary_directory(prefix="ssh-runner-", dir=None):
    import shutil

    path = new_private_directory(prefix, dir)
    try:
        yield str(path)
    finally:
        shutil.rmtree(path)


def new_private_directory(prefix="ssh-runner-", dir=None):
    path = Path(dir or tempfile.gettempdir()) / (prefix + uuid.uuid4().hex)
    private_directory(path)
    return path


@contextmanager
def private_temporary_file():
    if not WINDOWS:
        with tempfile.TemporaryFile() as handle:
            yield handle
        return
    with private_temporary_directory() as directory:
        path = Path(directory) / "data"
        fd = open_private(path, os.O_RDWR | os.O_CREAT | os.O_EXCL)
        with os.fdopen(fd, "w+b") as handle:
            yield handle


def archive_options(operation):
    """Inject local behavior without coupling standalone remote scripts to us."""
    if not WINDOWS:
        return {}
    from .archive import windows_member_key
    from .windows import inspect_archive_path

    if operation == "pack":
        return dict(
            opener=lambda path: open_regular(path, nofollow=True),
            inspect=inspect_archive_path,
            windows=True,
        )
    return dict(
        member_key=windows_member_key,
        publish=os.rename,
        windows=True,
        make_stage=lambda parent: new_private_directory(".ssh-directory-", parent),
    )
