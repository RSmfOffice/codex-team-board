from __future__ import annotations

import ctypes
import hashlib
import os
import subprocess
from pathlib import Path
from typing import Any, Mapping


PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE = 259
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
PROCESS_TERMINATE = 0x0001
PROCESS_SET_QUOTA = 0x0100


if os.name == "nt":
    from ctypes import wintypes

    class _FileTime(ctypes.Structure):
        _fields_ = [
            ("dwLowDateTime", wintypes.DWORD),
            ("dwHighDateTime", wintypes.DWORD),
        ]

    class _JobObjectBasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class _JobObjectExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JobObjectBasicLimitInformation),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]


def _instance_id(
    pid: int,
    creation_time: str,
    executable: str,
) -> str:
    material = "\0".join(
        (str(pid), str(creation_time), os.path.normcase(executable))
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _identity_from_parts(
    pid: int,
    creation_time: object,
    executable: object,
) -> dict[str, Any]:
    creation_text = str(creation_time or "").strip()
    executable_text = os.path.normpath(str(executable or "").strip())
    if pid <= 0 or not creation_text or not executable_text:
        return {}
    return {
        "pid": pid,
        "creation_time": creation_text,
        "executable": executable_text,
        "instance_id": _instance_id(pid, creation_text, executable_text),
    }


def process_identity(pid: int) -> dict[str, Any]:
    """Return a strong identity for a running process, or an empty mapping."""

    if pid <= 0:
        return {}
    if os.name == "nt":
        return _windows_process_identity(pid)
    return _unix_process_identity(pid)


def current_process_identity() -> dict[str, Any]:
    return process_identity(os.getpid())


def _windows_process_identity(pid: int) -> dict[str, Any]:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.DWORD,
    ]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_FileTime),
        ctypes.POINTER(_FileTime),
        ctypes.POINTER(_FileTime),
        ctypes.POINTER(_FileTime),
    ]
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return {}
    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return {}
        if exit_code.value != STILL_ACTIVE:
            return {}
        created = _FileTime()
        exited = _FileTime()
        kernel = _FileTime()
        user = _FileTime()
        if not kernel32.GetProcessTimes(
            handle,
            ctypes.byref(created),
            ctypes.byref(exited),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            return {}
        size = wintypes.DWORD(32768)
        executable = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(
            handle,
            0,
            executable,
            ctypes.byref(size),
        ):
            return {}
        creation_time = (
            int(created.dwHighDateTime) << 32
        ) | int(created.dwLowDateTime)
        return _identity_from_parts(
            pid,
            creation_time,
            executable.value,
        )
    finally:
        kernel32.CloseHandle(handle)


def _unix_process_identity(pid: int) -> dict[str, Any]:
    proc = Path("/proc") / str(pid)
    try:
        stat = (proc / "stat").read_text(encoding="utf-8")
        # The command name may contain spaces and parentheses. Fields after the
        # final ')' are stable; starttime is field 22 overall.
        fields = stat[stat.rfind(")") + 2 :].split()
        start_ticks = fields[19]
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(
            encoding="utf-8"
        ).strip()
        executable = os.readlink(proc / "exe")
        return _identity_from_parts(
            pid,
            f"{boot_id}:{start_ticks}",
            executable,
        )
    except (OSError, IndexError):
        pass
    try:
        result = subprocess.run(
            [
                "ps",
                "-o",
                "lstart=",
                "-o",
                "comm=",
                "-p",
                str(pid),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return {}
    if result.returncode != 0:
        return {}
    parts = result.stdout.strip().split(maxsplit=5)
    if len(parts) < 6:
        return {}
    return _identity_from_parts(pid, " ".join(parts[:5]), parts[5])


def identity_record(
    identity: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Normalize a process identity for persistence or comparison."""

    if not isinstance(identity, Mapping):
        return {}
    try:
        pid = int(identity.get("pid") or 0)
    except (TypeError, ValueError):
        return {}
    normalized = _identity_from_parts(
        pid,
        identity.get("creation_time")
        or identity.get("process_creation_time"),
        identity.get("executable")
        or identity.get("process_executable"),
    )
    supplied_instance = str(identity.get("instance_id") or "").strip()
    if (
        normalized
        and supplied_instance
        and supplied_instance != normalized["instance_id"]
    ):
        return {}
    return normalized


def persisted_process_identity(
    row: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if not isinstance(row, Mapping):
        return {}
    return identity_record(
        {
            "pid": row.get("pid") or row.get("process_pid"),
            "creation_time": row.get("process_creation_time"),
            "executable": row.get("process_executable"),
            "instance_id": row.get("process_instance_id"),
        }
    )


def process_identity_matches(
    expected: Mapping[str, Any] | None,
    current: Mapping[str, Any] | None = None,
) -> bool:
    normalized = identity_record(expected)
    if not normalized:
        return False
    actual = identity_record(current) if current is not None else process_identity(
        int(normalized["pid"])
    )
    if not actual:
        return False
    return (
        normalized["pid"] == actual["pid"]
        and normalized["creation_time"] == actual["creation_time"]
        and os.path.normcase(normalized["executable"])
        == os.path.normcase(actual["executable"])
        and normalized["instance_id"] == actual["instance_id"]
    )


def persisted_process_is_running(row: Mapping[str, Any] | None) -> bool:
    return process_identity_matches(persisted_process_identity(row))


def persisted_identity_fields(
    identity: Mapping[str, Any] | None,
) -> dict[str, Any]:
    normalized = identity_record(identity)
    if not normalized:
        return {
            "process_creation_time": None,
            "process_executable": None,
            "process_instance_id": None,
        }
    return {
        "process_creation_time": normalized["creation_time"],
        "process_executable": normalized["executable"],
        "process_instance_id": normalized["instance_id"],
    }


class ProcessJob:
    """Windows Job Object that owns a process tree for its full lifetime."""

    def __init__(self) -> None:
        self._handle: int | None = None
        if os.name != "nt":
            return
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [
            ctypes.c_void_p,
            wintypes.LPCWSTR,
        ]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
        information = _JobObjectExtendedLimitInformation()
        information.BasicLimitInformation.LimitFlags = (
            JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        )
        if not kernel32.SetInformationJobObject(
            handle,
            JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(information),
            ctypes.sizeof(information),
        ):
            error = ctypes.get_last_error()
            kernel32.CloseHandle(handle)
            raise OSError(error, "SetInformationJobObject failed")
        self._handle = int(handle)

    @property
    def handle(self) -> int | None:
        return self._handle

    @classmethod
    def attach(
        cls,
        process: subprocess.Popen[Any] | Any,
    ) -> ProcessJob | None:
        if os.name != "nt" or not getattr(process, "_handle", None):
            return None
        job = cls()
        try:
            job.assign(int(process.pid))
        except Exception:
            job.close()
            raise
        return job

    def assign(self, pid: int) -> None:
        if self._handle is None:
            return
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.AssignProcessToJobObject.argtypes = [
            wintypes.HANDLE,
            wintypes.HANDLE,
        ]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        access = (
            PROCESS_SET_QUOTA
            | PROCESS_TERMINATE
            | PROCESS_QUERY_LIMITED_INFORMATION
        )
        process_handle = kernel32.OpenProcess(access, False, pid)
        if not process_handle:
            raise OSError(
                ctypes.get_last_error(),
                f"OpenProcess failed for pid {pid}",
            )
        try:
            if not kernel32.AssignProcessToJobObject(
                self._handle,
                process_handle,
            ):
                raise OSError(
                    ctypes.get_last_error(),
                    f"AssignProcessToJobObject failed for pid {pid}",
                )
        finally:
            kernel32.CloseHandle(process_handle)

    def terminate(self, exit_code: int = 1) -> bool:
        if self._handle is None:
            return False
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.TerminateJobObject.argtypes = [
            wintypes.HANDLE,
            wintypes.UINT,
        ]
        kernel32.TerminateJobObject.restype = wintypes.BOOL
        return bool(
            kernel32.TerminateJobObject(
                self._handle,
                int(exit_code),
            )
        )

    def close(self) -> None:
        handle = self._handle
        self._handle = None
        if handle is None:
            return
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle(handle)

    def __enter__(self) -> ProcessJob:
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()


def pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION,
            False,
            pid,
        )
        if not handle:
            return False
        try:
            exit_code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(
                handle,
                ctypes.byref(exit_code),
            ):
                return False
            return exit_code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True
