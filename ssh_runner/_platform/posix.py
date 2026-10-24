"""POSIX implementations, imported only on POSIX clients."""

import fcntl
import os
import stat
from pathlib import Path


def _private(info, directory=False):
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise PermissionError(
            "Private paths must belong to the current user (0700/0600)."
        )


def private_directory(path, parents=False):
    path = Path(path)
    path.mkdir(mode=0o700, parents=parents, exist_ok=True)
    _private(path.lstat(), directory=True)


def open_private(path, flags=os.O_RDONLY):
    fd = os.open(str(path), flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        _private(os.fstat(fd))
        return fd
    except BaseException:
        os.close(fd)
        raise


def open_regular(path, nofollow=False):
    flags = os.O_RDONLY | os.O_NONBLOCK
    if nofollow:
        flags |= os.O_NOFOLLOW
    fd = os.open(str(path), flags)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise OSError("Local input must be a regular file.")
    return fd


def lock_file(fd):
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def unlock_file(fd):
    fcntl.flock(fd, fcntl.LOCK_UN)


def directory_link(target, referent):
    target.symlink_to(
        os.path.relpath(str(referent), str(target.parent)), target_is_directory=True
    )


def publish_file(source, target, overwrite=False):
    if overwrite:
        os.replace(source, target)
    else:
        os.link(source, target)
        os.unlink(source)
