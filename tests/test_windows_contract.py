import ctypes
from ctypes import wintypes as w
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase, mock

from collab import process_identity, runtime_transport


class WindowsContractTests(TestCase):
    def test_process_birth_uses_creation_filetime(self):
        kernel = SimpleNamespace()

        def times(_handle, *values):
            values[0]._obj.dwHighDateTime = 1
            values[0]._obj.dwLowDateTime = 2
            return 1

        kernel.GetProcessTimes = times
        self.assertEqual(process_identity._birth(kernel, 7), str((1 << 32) | 2))

    def test_job_assignment_precedes_primary_thread_resume(self):
        events = []
        kernel = SimpleNamespace()
        for name in ("CreateJobObjectW", "SetInformationJobObject", "QueryInformationJobObject",
                     "AssignProcessToJobObject", "TerminateJobObject", "CloseHandle", "GetProcessTimes",
                     "CreateToolhelp32Snapshot", "OpenThread", "ResumeThread"):
            setattr(kernel, name, lambda *_: 1)
        kernel.CreateJobObjectW = lambda *_: 10
        kernel.SetInformationJobObject = lambda *_: 1
        kernel.AssignProcessToJobObject = lambda *_: events.append("assigned") or 1
        kernel.GetProcessTimes = lambda *_: 1
        kernel.CloseHandle = lambda *_: 1
        process = SimpleNamespace(_handle=20, pid=30)
        cwd = Path.cwd()
        with mock.patch.object(runtime_transport.os, "name", "nt"), \
                mock.patch.object(runtime_transport.ctypes, "WinDLL", return_value=kernel, create=True), \
                mock.patch.object(runtime_transport.subprocess, "Popen", return_value=process) as popen:
            job = runtime_transport.WindowsJob()
            job._resume = lambda _pid: events.append("resumed")
            self.assertIs(job.start(["fixture"], cwd, {}), process)
            self.assertEqual(events, ["assigned", "resumed"])
            self.assertEqual(popen.call_args.kwargs["creationflags"], 0x4 | 0x08000000)
            job.close()
