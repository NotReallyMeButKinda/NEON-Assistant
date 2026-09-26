"""
sysinfo.py -- CPU load, memory use and battery for the status bar, straight from Win32 (no psutil).
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


class _MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD), ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong), ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong), ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong), ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]


class _SYSTEM_POWER_STATUS(ctypes.Structure):
    _fields_ = [("ACLineStatus", ctypes.c_ubyte), ("BatteryFlag", ctypes.c_ubyte), ("BatteryLifePercent", ctypes.c_ubyte),
                ("SystemStatusFlag", ctypes.c_ubyte), ("BatteryLifeTime", wintypes.DWORD),
                ("BatteryFullLifeTime", wintypes.DWORD)]


def ram_percent() -> int:
    """Percent of physical memory in use."""
    status = _MEMORYSTATUSEX()
    status.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
    _kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
    return int(status.dwMemoryLoad)


def _ticks() -> tuple[int, int, int]:
    idle, kernel, user = (wintypes.FILETIME() for _ in range(3))
    _kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user))
    to_int = lambda ft: (ft.dwHighDateTime << 32) | ft.dwLowDateTime          # noqa: E731
    return to_int(idle), to_int(kernel), to_int(user)


class CpuMeter:
    """Overall CPU load since the previous call to percent() (the first call reports since boot)."""

    def __init__(self) -> None:
        self._last = _ticks()

    def percent(self) -> int:
        idle, kernel, user = _ticks()
        d_idle, d_kernel, d_user = idle - self._last[0], kernel - self._last[1], user - self._last[2]
        self._last = (idle, kernel, user)
        total = d_kernel + d_user                  # kernel time already includes idle time
        return 0 if total <= 0 else max(0, min(100, round(100 * (total - d_idle) / total)))


def battery() -> tuple[int, bool] | None:
    """(percent, plugged in), or None on a desktop with no battery."""
    status = _SYSTEM_POWER_STATUS()
    if not _kernel32.GetSystemPowerStatus(ctypes.byref(status)):
        return None
    if status.BatteryFlag & 128 or status.BatteryLifePercent > 100:      # 128 = no battery, 255 = unknown
        return None
    return int(status.BatteryLifePercent), status.ACLineStatus == 1


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]


def process_names() -> set[str]:
    """Lower-case executable names of every running process, without '.exe' ({'chrome', 'discord', ...}).
    One Toolhelp snapshot: a few milliseconds."""
    _kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    snapshot = _kernel32.CreateToolhelp32Snapshot(0x00000002, 0)          # TH32CS_SNAPPROCESS
    if not snapshot or snapshot == wintypes.HANDLE(-1).value:
        return set()
    names: set[str] = set()
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
        ok = _kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            name = entry.szExeFile.lower()
            names.add(name[:-4] if name.endswith(".exe") else name)
            ok = _kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        _kernel32.CloseHandle(snapshot)
    return names
