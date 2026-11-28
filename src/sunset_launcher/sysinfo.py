"""Memory readings for the warm-up page."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def available_memory() -> int | None:
    """Bytes the system can give new work without swapping, or None when unknown."""
    if sys.platform.startswith("linux"):
        try:
            for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
        except (OSError, ValueError, IndexError):
            return None
        return None
    if sys.platform == "win32":
        import ctypes

        class MemoryStatus(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        windll = getattr(ctypes, "windll", None)
        if windll is None:
            return None
        status = MemoryStatus()
        status.dwLength = ctypes.sizeof(MemoryStatus)
        try:
            if windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return int(status.ullAvailPhys)
        except OSError:
            return None
    return None


def _linux_children(proc: Path) -> dict[int, list[int]]:
    children: dict[int, list[int]] = {}
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text(encoding="ascii", errors="replace")
            # The command name can contain spaces and parentheses, so read the fields after its last ")".
            parent = int(stat[stat.rindex(")") + 2:].split()[1])
        except (OSError, ValueError, IndexError):
            continue
        children.setdefault(parent, []).append(int(entry.name))
    return children


def _windows_children() -> dict[int, list[int]]:
    import ctypes
    from ctypes import wintypes

    windll = getattr(ctypes, "windll", None)
    if windll is None:
        return {}

    class Entry(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260),
        ]

    kernel32 = windll.kernel32
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    snapshot = kernel32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    if not snapshot or snapshot == wintypes.HANDLE(-1).value:
        return {}
    children: dict[int, list[int]] = {}
    try:
        entry = Entry()
        entry.dwSize = ctypes.sizeof(Entry)
        more = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            children.setdefault(entry.th32ParentProcessID, []).append(entry.th32ProcessID)
            more = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return children


def process_tree(root: int, proc: Path = Path("/proc")) -> list[int]:
    """A process and all its descendants, the root first.

    A Proton run is umu, then the Steam Linux Runtime container in a session of its own, then Wine and the
    layer, so the tree is followed by parent rather than by process group."""
    if sys.platform.startswith("linux"):
        children = _linux_children(proc) if proc.is_dir() else {}
    elif sys.platform == "win32":
        children = _windows_children()
    else:
        children = {}
    tree: list[int] = []
    pending = [root]
    seen: set[int] = set()
    while pending:
        pid = pending.pop(0)
        if pid in seen:
            continue
        seen.add(pid)
        tree.append(pid)
        pending.extend(child for child in children.get(pid, ()) if child != pid)
    return tree


def suspend_processes(pids: list[int], suspend: bool) -> list[int]:
    """Pause or continue processes. Returns the ones it reached.

    Windows counts suspends, so a process suspended twice needs two resumes: callers suspend each
    process once and continue exactly the ones that were suspended."""
    reached: list[int] = []
    if sys.platform == "win32":
        import ctypes

        windll = getattr(ctypes, "windll", None)
        if windll is None:
            return reached
        call = windll.ntdll.NtSuspendProcess if suspend else windll.ntdll.NtResumeProcess
        windll.kernel32.OpenProcess.restype = ctypes.c_void_p
        for pid in pids:
            handle = windll.kernel32.OpenProcess(0x0800, False, pid)  # PROCESS_SUSPEND_RESUME
            if handle:
                try:
                    if call(ctypes.c_void_p(handle)) == 0:
                        reached.append(pid)
                finally:
                    windll.kernel32.CloseHandle(ctypes.c_void_p(handle))
        return reached
    import signal

    for pid in pids:
        try:
            os.kill(pid, signal.SIGSTOP if suspend else signal.SIGCONT)
            reached.append(pid)
        except (ProcessLookupError, PermissionError):
            pass
    return reached


def process_tree_memory(root: int, proc: Path = Path("/proc")) -> int | None:
    """Resident bytes of a Linux process and all its descendants, or None where unsupported."""
    if not sys.platform.startswith("linux") or not proc.is_dir():
        return None
    page = os.sysconf("SC_PAGE_SIZE")
    total = 0
    found = False
    for pid in process_tree(root, proc):
        try:
            total += int((proc / str(pid) / "statm").read_text(encoding="ascii").split()[1]) * page
            found = True
        except (OSError, ValueError, IndexError):
            pass
    return total if found else None
