# -*- coding: utf-8 -*-
"""探测本机硬件配置，输出 JSON。供模型顾问使用，也可单独运行。"""
import json
import ctypes
from ctypes import wintypes, byref, Structure, sizeof
import sys, os
import gpu_monitor as G

# ---- 内存 ----
class MEMORYSTATUSEX(Structure):
    _fields_ = [("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

k = ctypes.windll.kernel32
ms = MEMORYSTATUSEX()
ms.dwLength = sizeof(MEMORYSTATUSEX)
k.GlobalMemoryStatusEx(byref(ms))

# ---- CPU ----
class SYSTEM_INFO(Structure):
    _fields_ = [("wProcessorArchitecture", wintypes.WORD),
                ("wReserved", wintypes.WORD), ("dwPageSize", wintypes.DWORD),
                ("lpMinimumApplicationAddress", ctypes.c_void_p),
                ("lpMaximumApplicationAddress", ctypes.c_void_p),
                ("dwActiveProcessorMask", ctypes.c_void_p),
                ("dwNumberOfProcessors", wintypes.DWORD),
                ("dwProcessorType", wintypes.DWORD),
                ("dwAllocationGranularity", wintypes.DWORD),
                ("wProcessorLevel", wintypes.WORD), ("wProcessorRevision", wintypes.WORD)]
si = SYSTEM_INFO()
k.GetNativeSystemInfo(byref(si))

# ---- 磁盘（系统盘）----
free = ctypes.c_ulonglong(); total = ctypes.c_ulonglong(); avail = ctypes.c_ulonglong()
sysdrive = os.environ.get("SystemDrive", "C:") + "\\"
k.GetDiskFreeSpaceExW(sysdrive, byref(free), byref(total), byref(avail))

# ---- GPU ----
gpus = G.enum_gpu_registry()
# 多显卡时取显存最大的那块（注册表枚举顺序不保证独显在前，避免选中核显）
gpus.sort(key=lambda g: -g["total_mb"])
# 驱动版本
drv = None
try:
    import winreg
    base = G.REG_GPU_CLASS
    rk = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base + "\\0000")
    try:
        drv, _ = winreg.QueryValueEx(rk, "DriverVersion")
    except OSError:
        pass
    winreg.CloseKey(rk)
except Exception:
    pass

# ---- CPU 型号（注册表）----
cpu_name = ""
try:
    import winreg
    rk = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                        r"HARDWARE\DESCRIPTION\System\CentralProcessor\0")
    cpu_name, _ = winreg.QueryValueEx(rk, "ProcessorNameString")
    winreg.CloseKey(rk)
except Exception:
    pass

out = {
    "gpu": {
        "name": gpus[0]["name"] if gpus else None,
        "vram_mb": round(gpus[0]["total_mb"]) if gpus else None,
        "driver": drv,
    },
    "ram_mb": ms.ullTotalPhys / (1024 * 1024),
    "ram_avail_mb": ms.ullAvailPhys / (1024 * 1024),
    "cpu": {
        "name": cpu_name.strip(),
        "cores": si.dwNumberOfProcessors,
    },
    "disk": {
        "total_gb": total.value / (1024**3),
        "free_gb": free.value / (1024**3),
        "path": sysdrive,
    },
}
if __name__ == "__main__":
    print(json.dumps(out, ensure_ascii=False, indent=2))
