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


def process_group_memory(group: int, proc: Path = Path("/proc")) -> int | None:
    """Resident bytes of every process in a Linux process group, or None where unsupported.

    Runner starts each command in a new session, so its process ID is also the group ID and
    the group covers the warm-up's compiler workers. Proton's shared wineserver can sit outside it.
    """
    if not sys.platform.startswith("linux") or not proc.is_dir():
        return None
    page = os.sysconf("SC_PAGE_SIZE")
    total = 0
    found = False
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text(encoding="ascii", errors="replace")
            # The command name can contain spaces and parentheses, so read the fields after its last ")".
            fields = stat[stat.rindex(")") + 2:].split()
            if int(fields[2]) != group:
                continue
            total += int((entry / "statm").read_text(encoding="ascii").split()[1]) * page
            found = True
        except (OSError, ValueError, IndexError):
            continue
    return total if found else None
