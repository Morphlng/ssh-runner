"""Pure Windows archive-name validation, also testable on POSIX."""

import re
from pathlib import PurePosixPath, PureWindowsPath

_DEVICES = re.compile(
    r"^(CON|CONIN\$|CONOUT\$|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])$", re.I
)


def windows_member_key(name):
    if (
        not name
        or "\\" in name
        or PureWindowsPath(name).drive
        or PurePosixPath(name).is_absolute()
        or any(ord(c) < 32 or c in '<>:"|?*' for c in name)
    ):
        raise ValueError("unsafe Windows archive path")
    path = PurePosixPath(name)
    for part in path.parts:
        if (
            part == ".."
            or part.endswith((".", " "))
            or _DEVICES.match(part.split(".", 1)[0].rstrip(" "))
        ):
            raise ValueError("unsafe Windows archive component")
    return str(path).casefold()
