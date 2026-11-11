"""Birth-checked process observation; never stop a process found only by name."""
from __future__ import annotations

import ctypes
from ctypes import wintypes as w
import errno
import os
from pathlib import Path
import sys

from .common import CollabError


def _windows():
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    k.OpenProcess.restype = w.HANDLE
    k.GetProcessTimes.argtypes = [w.HANDLE, ctypes.POINTER(w.FILETIME), ctypes.POINTER(w.FILETIME), ctypes.POINTER(w.FILETIME), ctypes.POINTER(w.FILETIME)]
    k.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
    k.CloseHandle.argtypes = [w.HANDLE]
    return k


def _birth(k, handle):
    values = [w.FILETIME() for _ in range(4)]
    if not k.GetProcessTimes(handle, *[ctypes.byref(v) for v in values]):
        raise CollabError("PROCESS_IDENTITY_UNKNOWN", "cannot read process creation time")
    return str((values[0].dwHighDateTime << 32) | values[0].dwLowDateTime)


class _DarwinBsdInfo(ctypes.Structure):
    # Darwin PROC_PIDTBSDINFO; use fixed-width C types on every host Python.
    _fields_ = [
        ("flags", ctypes.c_uint32), ("status", ctypes.c_uint32),
        ("xstatus", ctypes.c_uint32), ("pid", ctypes.c_uint32),
        ("ppid", ctypes.c_uint32), ("uid", ctypes.c_uint32),
        ("gid", ctypes.c_uint32), ("ruid", ctypes.c_uint32),
        ("rgid", ctypes.c_uint32), ("svuid", ctypes.c_uint32),
        ("svgid", ctypes.c_uint32), ("rfu", ctypes.c_uint32),
        ("comm", ctypes.c_char * 16), ("name", ctypes.c_char * 32),
        ("nfiles", ctypes.c_uint32), ("pgid", ctypes.c_uint32),
        ("pjobc", ctypes.c_uint32), ("tdev", ctypes.c_uint32),
        ("tpgid", ctypes.c_uint32), ("nice", ctypes.c_int32),
        ("start_sec", ctypes.c_uint64), ("start_usec", ctypes.c_uint64),
    ]


def _darwin_birth(pid):
    if ctypes.sizeof(_DarwinBsdInfo) != 136:
        raise CollabError("PROCESS_IDENTITY_UNKNOWN", "unexpected Darwin process metadata layout")
    libc = ctypes.CDLL(None, use_errno=True)
    libc.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
                                  ctypes.c_void_p, ctypes.c_int]
    libc.proc_pidinfo.restype = ctypes.c_int
    info = _DarwinBsdInfo()
    result = libc.proc_pidinfo(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info))
    if result != ctypes.sizeof(info):
        error = ctypes.get_errno()
        if error == errno.ESRCH:
            raise FileNotFoundError(pid)
        raise CollabError("PROCESS_IDENTITY_UNKNOWN", "cannot read Darwin process identity")
    if info.pid != pid:
        raise CollabError("PROCESS_IDENTITY_UNKNOWN", "Darwin returned a different process identity")
    if info.status == 5:  # SZOMB: identity remains queryable only until reaping.
        raise FileNotFoundError(pid)
    return f"{info.start_sec}.{info.start_usec:06d}"


def birth(pid):
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise CollabError("PROCESS_IDENTITY_UNKNOWN", "invalid process id")
    if os.name == "nt":
        k = _windows()
        handle = k.OpenProcess(0x1000, False, pid)
        if not handle:
            raise CollabError("PROCESS_IDENTITY_UNKNOWN", "cannot open process for observation")
        try:
            return _birth(k, handle)
        finally:
            k.CloseHandle(handle)
    if sys.platform == "darwin":
        return _darwin_birth(pid)
    path = Path(f"/proc/{pid}/stat")
    if Path("/proc").is_dir():
        return path.read_text().rsplit(")", 1)[1].split()[19]
    raise CollabError("PROCESS_IDENTITY_UNKNOWN", "no supported process birth observation on this host")


class ProcessWatch:
    """Observe the exact saved process identity, never a name-derived PID."""
    def __init__(self, pid, expected_birth):
        self.pid, self.expected_birth, self.handle, self.absent = pid, expected_birth, None, False
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0 or not isinstance(expected_birth, str):
            raise CollabError("PROCESS_IDENTITY_UNKNOWN", "invalid saved process identity")
        if os.name == "nt":
            self.k = _windows()
            self.handle = self.k.OpenProcess(0x1000 | 0x100000, False, pid)
            if not self.handle:
                if ctypes.get_last_error() == 87:
                    self.absent = True
                else:
                    raise CollabError("PROCESS_IDENTITY_UNKNOWN", "cannot observe original process")
            elif _birth(self.k, self.handle) != expected_birth:
                self.close()
                self.absent = True
        else:
            try:
                self.absent = birth(pid) != expected_birth
            except FileNotFoundError:
                self.absent = True

    def exited(self, timeout_ms=0):
        if self.absent:
            return True
        if os.name == "nt":
            answer = self.k.WaitForSingleObject(self.handle, timeout_ms)
            if answer == 0xFFFFFFFF:
                raise CollabError("PROCESS_IDENTITY_UNKNOWN", "process wait failed")
            return answer == 0
        try:
            return birth(self.pid) != self.expected_birth
        except FileNotFoundError:
            return True

    def close(self):
        if self.handle:
            self.k.CloseHandle(self.handle)
            self.handle = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
