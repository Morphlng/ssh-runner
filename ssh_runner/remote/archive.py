"""Safe directory archives; also sent verbatim to remote Python (stdlib only)."""

import ctypes
import hashlib
import json
import os
import shutil
import stat
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath


def digest(handle):
    value = hashlib.sha256()
    size = 0
    handle.seek(0)
    for chunk in iter(lambda: handle.read(65536), b""):
        value.update(chunk)
        size += len(chunk)
    handle.seek(0)
    return size, value.hexdigest()


def pack_directory(source, output):
    source = Path(source)
    if not stat.S_ISDIR(source.lstat().st_mode):
        raise ValueError("source must be a real directory, not a symlink")
    count = 0
    total = 0
    with tarfile.open(fileobj=output, mode="w", format=tarfile.PAX_FORMAT) as archive:

        def add(path, name):
            nonlocal count, total
            info = path.lstat()
            member = tarfile.TarInfo(name)
            member.mode = stat.S_IMODE(info.st_mode) & 0o777
            member.mtime = info.st_mtime
            if stat.S_ISDIR(info.st_mode):
                member.type = tarfile.DIRTYPE
                archive.addfile(member)
                for child in sorted(path.iterdir()):
                    add(child, child.name if name == "." else name + "/" + child.name)
            elif stat.S_ISREG(info.st_mode):
                # Never block on FIFOs or follow a final symlink if the tree changes.
                fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(fd, "rb") as handle:
                    current = os.fstat(handle.fileno())
                    if not stat.S_ISREG(current.st_mode):
                        raise ValueError("source changed during archive creation")
                    member.size = current.st_size
                    archive.addfile(member, handle)
                    after = os.fstat(handle.fileno())
                    if (current.st_size, current.st_mtime_ns) != (
                        after.st_size,
                        after.st_mtime_ns,
                    ):
                        raise ValueError("source changed during archive creation")
                count += 1
                total += member.size
            else:
                raise ValueError(
                    "directory transfer rejects symlinks and special files"
                )

        add(source, ".")
    output.flush()
    output.seek(0)
    return {"files": count, "file_bytes": total}


def publish_directory(staging, destination):
    """Atomic rename with kernel-enforced no-clobber, including empty directories."""
    library = ctypes.CDLL(None, use_errno=True)
    source, target = os.fsencode(staging), os.fsencode(destination)
    if sys.platform == "darwin" and hasattr(library, "renamex_np"):
        rename = library.renamex_np
        rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        result = rename(source, target, 0x00000004)  # RENAME_EXCL
    elif sys.platform.startswith("linux") and hasattr(library, "renameat2"):
        rename = library.renameat2
        rename.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        rename.restype = ctypes.c_int
        result = rename(-100, source, -100, target, 1)  # AT_FDCWD, RENAME_NOREPLACE
    else:
        raise ValueError(
            "atomic directory publication requires macOS or Linux rename-no-replace support"
        )
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def unpack_directory(handle, destination):
    destination = Path(destination).absolute()
    if os.path.lexists(destination):
        raise ValueError(
            "directory destination already exists; merging/overwrite is not supported"
        )
    stage = Path(tempfile.mkdtemp(prefix=".ssh-directory-", dir=destination.parent))
    metadata = []
    seen = set()
    count = total = 0
    try:
        with tarfile.open(fileobj=handle, mode="r:") as archive:
            for member in archive:
                path = PurePosixPath(member.name)
                if path.is_absolute() or ".." in path.parts or "\x00" in member.name:
                    raise ValueError("unsafe archive path")
                if not (member.isdir() or member.isreg()) or member.issparse():
                    raise ValueError(
                        "archive links and special/sparse files are not supported"
                    )
                key = str(path)
                if key in seen or (key == "." and not member.isdir()):
                    raise ValueError("duplicate or invalid archive path")
                seen.add(key)
                target = stage.joinpath(*path.parts)
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                if member.isdir():
                    target.mkdir(mode=0o700, exist_ok=True)
                else:
                    with archive.extractfile(member) as source, target.open(
                        "xb"
                    ) as output:
                        shutil.copyfileobj(source, output, 65536)
                    count += 1
                    total += member.size
                metadata.append((target, member.mode & 0o777, member.mtime))
        # Set directory modes last, so read-only parents do not prevent extraction.
        for target, mode, mtime in sorted(
            metadata, key=lambda item: len(item[0].parts), reverse=True
        ):
            os.chmod(target, mode)
            os.utime(target, (mtime, mtime))
        publish_directory(stage, destination)
        return {"files": count, "file_bytes": total, "path": str(destination)}
    finally:
        if stage.exists():
            # Even an archive containing mode-000 directories must be removable.
            os.chmod(stage, 0o700)
            for parent, directories, _ in os.walk(stage, topdown=True):
                os.chmod(parent, 0o700)
                for name in directories:
                    os.chmod(Path(parent) / name, 0o700)
            shutil.rmtree(stage)


def main(arguments):
    operation, path = arguments[:2]
    with tempfile.TemporaryFile() as archive:
        if operation == "receive":
            expected_size, expected_hash = int(arguments[2]), arguments[3]
            remaining = expected_size
            while remaining:
                chunk = sys.stdin.buffer.read(min(65536, remaining))
                if not chunk:
                    raise ValueError("incomplete directory archive")
                archive.write(chunk)
                remaining -= len(chunk)
            if sys.stdin.buffer.read(1):
                raise ValueError("directory archive size mismatch")
            size, sha256 = digest(archive)
            if size != expected_size or sha256 != expected_hash:
                raise ValueError("directory archive SHA-256 mismatch")
            data = unpack_directory(archive, path)
            data.update(bytes=size, sha256=sha256)
            print(json.dumps(data, ensure_ascii=False))
        elif operation == "send":
            pack_directory(path, archive)
            size, sha256 = digest(archive)
            print(arguments[2], size, sha256, file=sys.stderr, flush=True)
            shutil.copyfileobj(archive, sys.stdout.buffer, 65536)
        else:
            raise ValueError("unsupported directory operation")


if __name__ == "__main__":
    os.umask(0o077)
    try:
        main(sys.argv[1:])
    except Exception as error:
        print("directory transfer failed: " + str(error), file=sys.stderr)
        sys.exit(2)
