"""Native per-run process-tree containment for the driver subprocess.

No shell, process-name killing, retry, reconnect, or persisted-session resume.
The Job handle is deliberately non-inheritable: loss of its sole owner kills
the complete tree, including when the service terminates without finally.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes as w
import os
from pathlib import Path
import re
import signal
import subprocess


class TransportError(Exception):
    """Fixed public code; native exception messages never cross this boundary."""


def scrub(value: str) -> str:
    value = re.sub(r"(?i)(bearer\s+)[^\s\"']+", r"\1[REDACTED]", value)
    value = re.sub(r"\b(?:sk-|sk_|ghp_|gho_)[A-Za-z0-9_\-]+", "[REDACTED]", value)
    return re.sub(r"(?i)(api[_-]?key|authorization|access[_-]?token)([\"'\s:=]+)[^\s,\"'}]+",
                  r"\1\2[REDACTED]", value)


class WindowsJob:
    def __init__(self):
        if os.name != "nt":
            raise TransportError("WINDOWS_JOB_REQUIRED")
        self.handle = None
        self.k = k = ctypes.WinDLL("kernel32", use_last_error=True)
        signatures = {
            "CreateJobObjectW": ([ctypes.c_void_p, w.LPCWSTR], w.HANDLE),
            "SetInformationJobObject": ([w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD], w.BOOL),
            "QueryInformationJobObject": ([w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD, ctypes.c_void_p], w.BOOL),
            "AssignProcessToJobObject": ([w.HANDLE, w.HANDLE], w.BOOL),
            "TerminateJobObject": ([w.HANDLE, w.UINT], w.BOOL),
            "CloseHandle": ([w.HANDLE], w.BOOL),
            "GetProcessTimes": ([w.HANDLE, *([ctypes.POINTER(w.FILETIME)] * 4)], w.BOOL),
            "CreateToolhelp32Snapshot": ([w.DWORD, w.DWORD], w.HANDLE),
            "OpenThread": ([w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
            "ResumeThread": ([w.HANDLE], w.DWORD),
        }
        for name, (args, result) in signatures.items():
            getattr(k, name).argtypes, getattr(k, name).restype = args, result
        self.handle = k.CreateJobObjectW(None, None)
        if not self.handle:
            raise TransportError("JOB_CREATE_FAILED")

        class Basic(ctypes.Structure):
            _fields_ = [("processTime", ctypes.c_longlong), ("jobTime", ctypes.c_longlong),
                        ("flags", w.DWORD), ("min", ctypes.c_size_t), ("max", ctypes.c_size_t),
                        ("processes", w.DWORD), ("affinity", ctypes.c_size_t),
                        ("priority", w.DWORD), ("scheduling", w.DWORD)]

        class Extended(ctypes.Structure):
            _fields_ = [("basic", Basic), ("io", ctypes.c_ulonglong * 6),
                        ("memory", ctypes.c_size_t * 4)]

        info = Extended()
        info.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k.SetInformationJobObject(self.handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            self.close()
            raise TransportError("JOB_LIMIT_FAILED")

    def start(self, argv: list[str], cwd: Path, env: dict, before_resume=None):
        p = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             creationflags=0x4 | 0x08000000, close_fds=True)
        try:
            if not self.k.AssignProcessToJobObject(self.handle, int(p._handle)):
                raise TransportError("JOB_ASSIGN_FAILED")
            times = [w.FILETIME() for _ in range(4)]
            if not self.k.GetProcessTimes(int(p._handle), *[ctypes.byref(t) for t in times]):
                raise TransportError("PROCESS_BIRTH_FAILED")
            identity = str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)
            if before_resume:
                before_resume(p.pid, identity)
            self._resume(p.pid)
            return p
        except BaseException:
            # Even assignment failure leaves an exact, suspended process handle.
            p.kill()
            p.wait(timeout=10)
            for stream in (p.stdin, p.stdout, p.stderr):
                stream.close()
            raise

    def _resume(self, pid):
        class Entry(ctypes.Structure):
            _fields_ = [("size", w.DWORD), ("usage", w.DWORD), ("tid", w.DWORD),
                        ("pid", w.DWORD), ("base", w.LONG), ("delta", w.LONG), ("flags", w.DWORD)]
        self.k.Thread32First.argtypes = [w.HANDLE, ctypes.POINTER(Entry)]
        self.k.Thread32Next.argtypes = [w.HANDLE, ctypes.POINTER(Entry)]
        snap = self.k.CreateToolhelp32Snapshot(4, 0)
        if snap == ctypes.c_void_p(-1).value:
            raise TransportError("THREAD_SNAPSHOT_FAILED")
        try:
            entry = Entry()
            entry.size = ctypes.sizeof(entry)
            ok = self.k.Thread32First(snap, ctypes.byref(entry))
            while ok:
                if entry.pid == pid:
                    handle = self.k.OpenThread(2, False, entry.tid)
                    if not handle:
                        raise TransportError("THREAD_OPEN_FAILED")
                    try:
                        if self.k.ResumeThread(handle) == 0xFFFFFFFF:
                            raise TransportError("THREAD_RESUME_FAILED")
                        return
                    finally:
                        self.k.CloseHandle(handle)
                ok = self.k.Thread32Next(snap, ctypes.byref(entry))
            raise TransportError("PRIMARY_THREAD_MISSING")
        finally:
            self.k.CloseHandle(snap)

    def active(self) -> int:
        class Accounting(ctypes.Structure):
            _fields_ = [("times", ctypes.c_longlong * 4), ("faults", w.DWORD),
                        ("total", w.DWORD), ("active", w.DWORD), ("terminated", w.DWORD)]
        info = Accounting()
        if not self.k.QueryInformationJobObject(self.handle, 1, ctypes.byref(info), ctypes.sizeof(info), None):
            raise TransportError("JOB_QUERY_FAILED")
        return info.active

    def terminate(self):
        if not self.k.TerminateJobObject(self.handle, 197):
            raise TransportError("JOB_TERMINATE_FAILED")

    def close(self):
        if self.handle:
            self.k.CloseHandle(self.handle)
            self.handle = None


class PosixJob:
    """Contain a worker tree in its own POSIX session/process group.

    A tiny launcher stops before exec for the durable birth callback. A second,
    independent watcher owns the read side of a pipe: abnormal owner exit closes
    the write side and the watcher kills the whole worker group. Normal close
    terminates first and then sends a byte so no late signal can hit a reused
    process-group id. Worker processes never inherit the control descriptor.
    """
    _LAUNCHER = (
        "import os,signal,sys;"
        "os.kill(os.getpid(),signal.SIGSTOP);"
        "os.execvpe(sys.argv[1],sys.argv[1:],os.environ)"
    )
    _MONITOR = """import os,signal,sys
fd,pgid=int(sys.argv[1]),int(sys.argv[2])
try:
    normal=os.read(fd,1)
finally:
    os.close(fd)
if not normal:
    try:
        os.killpg(pgid,signal.SIGKILL)
    except ProcessLookupError:
        pass
"""

    def __init__(self):
        if os.name != "posix":
            raise TransportError("POSIX_JOB_REQUIRED")
        self.pgid = None
        self.process = None
        self.closed = False
        self._monitor = None
        self._monitor_write = None

    def _start_monitor(self):
        import sys
        read_fd, write_fd = os.pipe()
        self._monitor_write = write_fd
        try:
            self._monitor = subprocess.Popen(
                [sys.executable, "-I", "-c", self._MONITOR, str(read_fd), str(self.pgid)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, close_fds=True,
                pass_fds=(read_fd,), start_new_session=True,
            )
        finally:
            os.close(read_fd)

    def _stop_monitor(self, normal):
        if self._monitor_write is not None:
            if normal:
                try:
                    os.write(self._monitor_write, b"C")
                except OSError:
                    pass
            os.close(self._monitor_write)
            self._monitor_write = None
        if self._monitor is not None:
            try:
                self._monitor.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._monitor.kill()
                self._monitor.wait(timeout=10)
            self._monitor = None

    def start(self, argv: list[str], cwd: Path, env: dict, before_resume=None):
        if self.closed or self.process is not None:
            raise TransportError("JOB_ALREADY_USED")
        import sys
        p = subprocess.Popen(
            [sys.executable, "-I", "-c", self._LAUNCHER, *map(str, argv)],
            cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, close_fds=True, start_new_session=True,
        )
        self.process, self.pgid = p, p.pid
        try:
            waited, status = os.waitpid(p.pid, os.WUNTRACED)
            if waited != p.pid or not os.WIFSTOPPED(status):
                raise TransportError("PROCESS_START_FAILED")
            self._start_monitor()
            from .process_identity import birth
            identity = birth(p.pid)
            if before_resume:
                before_resume(p.pid, identity)
            os.killpg(self.pgid, signal.SIGCONT)
            return p
        except BaseException:
            try:
                os.killpg(self.pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self._stop_monitor(False)
            p.wait(timeout=10)
            for stream in (p.stdin, p.stdout, p.stderr):
                stream.close()
            raise

    def _members(self):
        result = subprocess.run(
            ["/bin/ps", "-axo", "pid=,pgid=,stat="], capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=5,
            check=False, stdin=subprocess.DEVNULL,
        )
        if result.returncode != 0:
            raise TransportError("JOB_QUERY_FAILED")
        members = []
        for line in result.stdout.splitlines():
            fields = line.split(None, 2)
            if len(fields) != 3:
                continue
            try:
                pid, pgid = int(fields[0]), int(fields[1])
            except ValueError:
                continue
            if pgid == self.pgid:
                members.append((pid, fields[2]))
        return members

    def active(self) -> int:
        if self.pgid is None:
            return 0
        return sum(not stat.startswith("Z") for _, stat in self._members())

    def terminate(self):
        if self.pgid is None or not self.active():
            return
        try:
            os.killpg(self.pgid, signal.SIGKILL)
        except ProcessLookupError:
            return
        except OSError:
            raise TransportError("JOB_TERMINATE_FAILED") from None

    def close(self):
        if self.closed:
            return
        error = None
        try:
            if self.pgid is not None and self.active():
                self.terminate()
        except BaseException as exc:
            error = exc
        self._stop_monitor(error is None)
        self.closed = True
        self.process = None
        if error is not None:
            raise error


NativeJob = WindowsJob if os.name == "nt" else PosixJob
