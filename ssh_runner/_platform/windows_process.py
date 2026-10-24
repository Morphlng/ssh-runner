"""Suspended launch into a kill-on-close Job Object, without third-party code."""

import ctypes
import subprocess
from ctypes import wintypes as w

from .windows import INVALID_HANDLE, CloseHandle, bind, checked, kernel

LAUNCH_CLEANUP_SECONDS = 2


class BasicLimit(ctypes.Structure):
    _fields_ = [
        ("process_time", ctypes.c_int64),
        ("job_time", ctypes.c_int64),
        ("flags", w.DWORD),
        ("min_working", ctypes.c_size_t),
        ("max_working", ctypes.c_size_t),
        ("active", w.DWORD),
        ("affinity", ctypes.c_size_t),
        ("priority", w.DWORD),
        ("scheduling", w.DWORD),
    ]


class IoCounters(ctypes.Structure):
    _fields_ = [
        (name, ctypes.c_uint64)
        for name in (
            "read_ops",
            "write_ops",
            "other_ops",
            "read_bytes",
            "write_bytes",
            "other_bytes",
        )
    ]


class ExtendedLimit(ctypes.Structure):
    _fields_ = [
        ("basic", BasicLimit),
        ("io", IoCounters),
        ("process_memory", ctypes.c_size_t),
        ("job_memory", ctypes.c_size_t),
        ("peak_process", ctypes.c_size_t),
        ("peak_job", ctypes.c_size_t),
    ]


class ThreadEntry(ctypes.Structure):
    _fields_ = [
        ("size", w.DWORD),
        ("usage", w.DWORD),
        ("tid", w.DWORD),
        ("pid", w.DWORD),
        ("priority", w.LONG),
        ("delta", w.LONG),
        ("flags", w.DWORD),
    ]


CreateJob = bind(kernel, "CreateJobObjectW", [w.LPVOID, w.LPCWSTR], w.HANDLE)
SetJob = bind(
    kernel, "SetInformationJobObject", [w.HANDLE, ctypes.c_int, w.LPVOID, w.DWORD]
)
AssignJob = bind(kernel, "AssignProcessToJobObject", [w.HANDLE, w.HANDLE])
TerminateJob = bind(kernel, "TerminateJobObject", [w.HANDLE, w.UINT])
OpenProcess = bind(kernel, "OpenProcess", [w.DWORD, w.BOOL, w.DWORD], w.HANDLE)
Snapshot = bind(kernel, "CreateToolhelp32Snapshot", [w.DWORD, w.DWORD], w.HANDLE)
ThreadFirst = bind(kernel, "Thread32First", [w.HANDLE, ctypes.POINTER(ThreadEntry)])
ThreadNext = bind(kernel, "Thread32Next", [w.HANDLE, ctypes.POINTER(ThreadEntry)])
OpenThread = bind(kernel, "OpenThread", [w.DWORD, w.BOOL, w.DWORD], w.HANDLE)
ResumeThread = bind(kernel, "ResumeThread", [w.HANDLE], w.DWORD)


def _resume_primary(pid):
    # Popen closes CreateProcess's primary-thread handle. Toolhelp gives us a
    # documented way to reopen it; no child code runs before ResumeThread.
    snapshot = Snapshot(0x00000004, 0)
    if snapshot == INVALID_HANDLE:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        entry = ThreadEntry()
        entry.size = ctypes.sizeof(entry)
        present = ThreadFirst(snapshot, ctypes.byref(entry))
        while present:
            if entry.pid == pid:
                thread = checked(OpenThread(0x0002, False, entry.tid))
                try:
                    if ResumeThread(thread) == 0xFFFFFFFF:
                        raise ctypes.WinError(ctypes.get_last_error())
                    return
                finally:
                    CloseHandle(thread)
            present = ThreadNext(snapshot, ctypes.byref(entry))
        raise OSError("Cannot find suspended SSH primary thread.")
    finally:
        CloseHandle(snapshot)


class WindowsProcess(subprocess.Popen):
    def __init__(self, *args, **kwargs):
        self._job = checked(CreateJob(None, None))
        try:
            limits = ExtendedLimit()
            limits.basic.flags = 0x00002000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            checked(SetJob(self._job, 9, ctypes.byref(limits), ctypes.sizeof(limits)))
            kwargs.pop("start_new_session", None)
            kwargs["creationflags"] = (
                kwargs.get("creationflags", 0) | 0x00000004 | 0x08000000
            )
            super().__init__(*args, **kwargs)
            handle = checked(OpenProcess(0x0101, False, self.pid))
            try:
                checked(AssignJob(self._job, handle))
            finally:
                CloseHandle(handle)
            _resume_primary(self.pid)
        except BaseException:
            try:
                if getattr(self, "pid", None) is not None:
                    try:
                        subprocess.Popen.kill(self)
                        self.wait(timeout=LAUNCH_CLEANUP_SECONDS)
                    finally:
                        for stream in (self.stdin, self.stdout, self.stderr):
                            if stream is not None:
                                stream.close()
            finally:
                self.close_job()
            raise

    def terminate_tree(self):
        if self._job is not None:
            checked(TerminateJob(self._job, 1))

    def close_job(self):
        if self._job is not None:
            CloseHandle(self._job)
            self._job = None

    def __exit__(self, *args):
        # Kill live descendants before Popen's context manager waits/closes
        # streams; exceptional exits must not wait on an unowned pipe writer.
        self.close_job()
        return super().__exit__(*args)
