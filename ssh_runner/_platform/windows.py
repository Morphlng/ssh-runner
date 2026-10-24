"""Handle-based Windows files, ACLs, locks and directory junctions (stdlib only)."""

import ctypes
import msvcrt
import os
import struct
from contextlib import contextmanager
from ctypes import wintypes as w
from functools import lru_cache
from pathlib import Path

from .archive import windows_member_key

kernel = ctypes.WinDLL("kernel32", use_last_error=True)
security = ctypes.WinDLL("advapi32", use_last_error=True)
INVALID_HANDLE = ctypes.c_void_p(-1).value
READ_CONTROL = 0x20000
REPARSE_POINT = 0x400
DIRECTORY = 0x10
OPEN_REPARSE = 0x00200000
BACKUP_SEMANTICS = 0x02000000


def bind(library, name, arguments, result=w.BOOL):
    function = getattr(library, name)
    function.argtypes, function.restype = arguments, result
    return function


class SecurityAttributes(ctypes.Structure):
    _fields_ = [("length", w.DWORD), ("descriptor", w.LPVOID), ("inherit", w.BOOL)]


class AttributeTag(ctypes.Structure):
    _fields_ = [("attributes", w.DWORD), ("tag", w.DWORD)]


class Overlapped(ctypes.Structure):
    _fields_ = [
        ("internal", ctypes.c_size_t),
        ("high", ctypes.c_size_t),
        ("offset", w.DWORD),
        ("offset_high", w.DWORD),
        ("event", w.HANDLE),
    ]


class AclSize(ctypes.Structure):
    _fields_ = [("count", w.DWORD), ("used", w.DWORD), ("free", w.DWORD)]


CloseHandle = bind(kernel, "CloseHandle", [w.HANDLE])
LocalFree = bind(kernel, "LocalFree", [w.LPVOID], w.LPVOID)
CreateFile = bind(
    kernel,
    "CreateFileW",
    [
        w.LPCWSTR,
        w.DWORD,
        w.DWORD,
        ctypes.POINTER(SecurityAttributes),
        w.DWORD,
        w.DWORD,
        w.HANDLE,
    ],
    w.HANDLE,
)
GetAttributes = bind(
    kernel, "GetFileInformationByHandleEx", [w.HANDLE, ctypes.c_int, w.LPVOID, w.DWORD]
)
GetFileType = bind(kernel, "GetFileType", [w.HANDLE], w.DWORD)
CreateDirectory = bind(
    kernel, "CreateDirectoryW", [w.LPCWSTR, ctypes.POINTER(SecurityAttributes)]
)
OpenToken = bind(
    security, "OpenProcessToken", [w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE)]
)
GetCurrentProcess = bind(kernel, "GetCurrentProcess", [], w.HANDLE)
GetTokenInformation = bind(
    security,
    "GetTokenInformation",
    [w.HANDLE, ctypes.c_int, w.LPVOID, w.DWORD, ctypes.POINTER(w.DWORD)],
)
SidToString = bind(
    security, "ConvertSidToStringSidW", [w.LPVOID, ctypes.POINTER(w.LPWSTR)]
)
StringToDescriptor = bind(
    security,
    "ConvertStringSecurityDescriptorToSecurityDescriptorW",
    [w.LPCWSTR, w.DWORD, ctypes.POINTER(w.LPVOID), ctypes.POINTER(w.DWORD)],
)
GetSecurity = bind(
    security,
    "GetSecurityInfo",
    [
        w.HANDLE,
        ctypes.c_int,
        w.DWORD,
        ctypes.POINTER(w.LPVOID),
        ctypes.POINTER(w.LPVOID),
        ctypes.POINTER(w.LPVOID),
        ctypes.POINTER(w.LPVOID),
        ctypes.POINTER(w.LPVOID),
    ],
    w.DWORD,
)
GetAclInformation = bind(
    security, "GetAclInformation", [w.LPVOID, w.LPVOID, w.DWORD, ctypes.c_int]
)
GetAce = bind(security, "GetAce", [w.LPVOID, w.DWORD, ctypes.POINTER(w.LPVOID)])
LockFile = bind(
    kernel,
    "LockFileEx",
    [w.HANDLE, w.DWORD, w.DWORD, w.DWORD, w.DWORD, ctypes.POINTER(Overlapped)],
)
UnlockFile = bind(
    kernel,
    "UnlockFileEx",
    [w.HANDLE, w.DWORD, w.DWORD, w.DWORD, ctypes.POINTER(Overlapped)],
)
DeviceIoControl = bind(
    kernel,
    "DeviceIoControl",
    [
        w.HANDLE,
        w.DWORD,
        w.LPVOID,
        w.DWORD,
        w.LPVOID,
        w.DWORD,
        ctypes.POINTER(w.DWORD),
        w.LPVOID,
    ],
)


def checked(value):
    if not value:
        raise ctypes.WinError(ctypes.get_last_error())
    return value


def sid_string(sid):
    result = w.LPWSTR()
    checked(SidToString(sid, ctypes.byref(result)))
    try:
        return result.value
    finally:
        LocalFree(result)


@lru_cache(maxsize=1)
def current_sid():
    token = w.HANDLE()
    checked(OpenToken(GetCurrentProcess(), 0x0008, ctypes.byref(token)))
    try:
        length = w.DWORD()
        GetTokenInformation(token, 1, None, 0, ctypes.byref(length))
        buffer = ctypes.create_string_buffer(length.value)
        checked(GetTokenInformation(token, 1, buffer, length, ctypes.byref(length)))
        return sid_string(ctypes.cast(buffer, ctypes.POINTER(w.LPVOID))[0])
    finally:
        CloseHandle(token)


@contextmanager
def private_attributes():
    descriptor = w.LPVOID()
    # A protected DACL is installed at creation, before any secret is written.
    sddl = "O:%sD:P(A;OICI;FA;;;%s)(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)" % (
        current_sid(),
        current_sid(),
    )
    checked(StringToDescriptor(sddl, 1, ctypes.byref(descriptor), None))
    try:
        yield SecurityAttributes(ctypes.sizeof(SecurityAttributes), descriptor, False)
    finally:
        LocalFree(descriptor)


def attributes(handle):
    info = AttributeTag()
    checked(GetAttributes(handle, 9, ctypes.byref(info), ctypes.sizeof(info)))
    return info.attributes


def check_private_handle(handle, directory=False):
    flags = attributes(handle)
    if (
        flags & REPARSE_POINT
        or bool(flags & DIRECTORY) != directory
        or GetFileType(handle) != 1
    ):
        raise PermissionError(
            "Private paths must be real disk files/directories, not reparse points."
        )
    owner, dacl, descriptor = w.LPVOID(), w.LPVOID(), w.LPVOID()
    error = GetSecurity(
        handle,
        1,
        0x00000005,
        ctypes.byref(owner),
        None,
        ctypes.byref(dacl),
        None,
        ctypes.byref(descriptor),
    )
    if error:
        raise ctypes.WinError(error)
    try:
        if sid_string(owner) != current_sid() or not dacl:
            raise PermissionError(
                "Private paths must be owned by the current user with a restricted ACL."
            )
        size = AclSize()
        checked(GetAclInformation(dacl, ctypes.byref(size), ctypes.sizeof(size), 2))
        trusted = {current_sid(), "S-1-5-18", "S-1-5-32-544"}
        for index in range(size.count):
            ace = w.LPVOID()
            checked(GetAce(dacl, index, ctypes.byref(ace)))
            kind = ctypes.c_ubyte.from_address(ace.value).value
            if kind == 1:  # ACCESS_DENIED_ACE cannot widen access.
                continue
            if kind != 0 or sid_string(ace.value + 8) not in trusted:
                raise PermissionError(
                    "Private path ACL grants access outside the current user, SYSTEM and Administrators."
                )
    finally:
        LocalFree(descriptor)


def _open(path, access, disposition=3, private=False):
    path = os.path.abspath(os.fspath(path))
    # Alternate streams and device namespaces cannot be private ordinary files.
    if ":" in path[2:] or path.startswith(("\\\\", "\\\\?\\", "\\\\.\\")):
        raise PermissionError(
            "Use a local filesystem path, without alternate streams or device namespaces."
        )
    try:
        # Validate the tail before opening: Windows aliases such as NUL.txt or
        # CONIN$ must never reach a potentially blocking device open.
        windows_member_key(Path(path).name)
    except ValueError:
        raise PermissionError(
            "Local input must not name a Windows device or alternate stream."
        ) from None
    with private_attributes() if private else _no_attributes() as sa:
        handle = CreateFile(
            path,
            access,
            0x00000007,
            ctypes.byref(sa) if sa else None,
            disposition,
            OPEN_REPARSE | BACKUP_SEMANTICS,
            None,
        )
        error = ctypes.get_last_error() if handle == INVALID_HANDLE else 0
    if handle == INVALID_HANDLE:
        raise ctypes.WinError(error)
    return handle


@contextmanager
def _no_attributes():
    yield None


def private_directory(path, parents=False):
    path = Path(path).absolute()
    if parents and not path.parent.exists():
        private_directory(path.parent, parents=True)
    with private_attributes() as sa:
        if not CreateDirectory(str(path), ctypes.byref(sa)):
            error = ctypes.get_last_error()
            if error != 183:
                raise ctypes.WinError(error)
    handle = _open(path, READ_CONTROL)
    try:
        check_private_handle(handle, directory=True)
    finally:
        CloseHandle(handle)


def open_private(path, flags=os.O_RDONLY):
    access = READ_CONTROL | (0x80000000 if not flags & os.O_WRONLY else 0)
    if flags & (os.O_WRONLY | os.O_RDWR):
        access |= 0x40000000
    disposition = 1 if flags & os.O_EXCL else 4 if flags & os.O_CREAT else 3
    handle = _open(path, access, disposition, private=True)
    try:
        check_private_handle(handle)
        return msvcrt.open_osfhandle(
            handle, flags & (os.O_RDONLY | os.O_WRONLY | os.O_RDWR) | os.O_BINARY
        )
    except BaseException:
        CloseHandle(handle)
        raise


def open_regular(path, nofollow=False):
    # Reparse points are rejected even when nofollow=False: local inputs must be
    # ordinary files, not device streams or mutable filesystem redirects.
    handle = _open(path, 0x80000000)
    try:
        if attributes(handle) & (REPARSE_POINT | DIRECTORY) or GetFileType(handle) != 1:
            raise OSError("Local input must be a regular file without reparse points.")
        return msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        CloseHandle(handle)
        raise


def lock_file(fd):
    if not LockFile(
        msvcrt.get_osfhandle(fd), 0x00000003, 0, 1, 0, ctypes.byref(Overlapped())
    ):
        error = ctypes.get_last_error()
        if error == 33:
            raise BlockingIOError("State file is locked.")
        raise ctypes.WinError(error)


def unlock_file(fd):
    checked(UnlockFile(msvcrt.get_osfhandle(fd), 0, 1, 0, ctypes.byref(Overlapped())))


def publish_file(source, target, overwrite=False):
    (os.replace if overwrite else os.rename)(source, target)


def inspect_archive_path(path):
    info = path.lstat()
    if info.st_file_attributes & REPARSE_POINT:
        raise ValueError("directory transfer rejects reparse points and junctions")
    return info


def directory_link(target, referent):
    referent = str(Path(referent).resolve())
    if referent.startswith("\\\\"):
        raise OSError("Skill junctions require a local target.")
    substitute = ("\\??\\" + referent).encode("utf-16-le")
    display = referent.encode("utf-16-le")
    names = substitute + b"\0\0" + display + b"\0\0"
    payload = (
        struct.pack("<HHHH", 0, len(substitute), len(substitute) + 2, len(display))
        + names
    )
    data = struct.pack("<LHH", 0xA0000003, len(payload), 0) + payload
    target.mkdir()
    handle = None
    try:
        handle = _open(target, 0x40000000)
        buffer = ctypes.create_string_buffer(data)
        returned = w.DWORD()
        checked(
            DeviceIoControl(
                handle,
                0x000900A4,
                buffer,
                len(data),
                None,
                0,
                ctypes.byref(returned),
                None,
            )
        )
    except BaseException:
        if handle is not None:
            CloseHandle(handle)
            handle = None
        target.rmdir()
        raise
    finally:
        if handle is not None:
            CloseHandle(handle)
