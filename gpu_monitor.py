# -*- coding: utf-8 -*-
"""
GPU 浮动监视面板
================
功能：
  * 无边框置顶悬浮窗，可拖动，实时显示 GPU 利用率 / 显存 / 温度 / 功耗
  * 60 点滚动折线图
  * 列出所有占用 GPU 的进程，可安全地结束它们（带系统进程保护 + 二次确认）
  * 三种隐藏方式：双击标题栏收成悬浮球 / 最小化到系统托盘 / Ctrl+Alt+G 全局呼出

数据源（双源自动降级）：
  1. nvidia-smi        -> 显存、温度、功耗、每进程显存（准确）
  2. Windows PDH 性能计数器 -> 每进程 GPU 引擎利用率（nvidia-smi 不可用时兜底）

依赖：仅 Python 标准库 + tkinter + Pillow（画托盘图标）。
"""

import os
import re
import sys
import json
import time
import ctypes
import threading
import subprocess
import shutil
import queue
import traceback
import winreg
from ctypes import wintypes, byref, POINTER
from dataclasses import dataclass, field
from typing import List, Dict, Optional

import tkinter as tk
from tkinter import ttk, messagebox

# ---------------------------------------------------------------- 常量 / 路径

APP_NAME = "GPU 监视器"
APP_VERSION = "1.1.0"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "gpu_monitor_config.json")
ICON_PATH = os.path.join(BASE_DIR, "_gpu_tray.ico")

FONT = "Microsoft YaHei UI"
MONO = "Consolas"

# 窗口尺寸：无边框窗口无法由系统缩放，全部靠右下角抓手手动改
DEFAULT_W, DEFAULT_H = 356, 500        # 主面板默认尺寸
MIN_W, MIN_H = 300, 340                # 主面板最小尺寸
ADV_DEFAULT_W, ADV_DEFAULT_H = 460, 640  # 模型顾问默认尺寸
ADV_MIN_W, ADV_MIN_H = 420, 380          # 模型顾问最小尺寸

# 关键系统进程：禁止结束（小写名）
PROTECTED_NAMES = {
    "system", "system idle process", "registry", "memory compression",
    "smss.exe", "csrss.exe", "wininit.exe", "winlogon.exe", "services.exe",
    "lsass.exe", "lsaiso.exe", "dwm.exe", "explorer.exe", "sihost.exe",
    "fontdrvhost.exe", "ctfmon.exe", "taskhostw.exe", "runtimebroker.exe",
    "svchost.exe", "audiodg.exe", "conhost.exe", "wudfhost.exe",
    "spoolsv.exe", "searchindexer.exe", "wmiprvse.exe", "dllhost.exe",
    "shellexperiencehost.exe", "startmenuexperiencehost.exe",
    "applicationframehost.exe", "textinputhost.exe", "lockapp.exe",
    "userinit.exe", "securityhealthservice.exe", "securityhealthsystray.exe",
    "searchapp.exe", "searchhost.exe", "widgets.exe", "widgetservice.exe",
}
PROTECTED_PIDS = {0, 4}

# 大模型模式白名单: 这些即使占了 GPU 也不结束 (系统进程 + 推理程序本身 + 驱动)
LLM_MODE_WHITELIST = PROTECTED_NAMES | {
    "python.exe", "pythonw.exe",          # 推理程序多为 python (ComfyUI/llama.cpp)
    "nvvsvc.exe", "nvcontainer.exe", "nvidia web helper.exe",
    "cmd.exe", "powershell.exe", "windowsterminal.exe", "openconsole.exe",
}

# Win32 常量
PROCESS_TERMINATE = 0x0001
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TH32CS_SNAPPROCESS = 0x00000002
WM_HOTKEY = 0x0312
WM_DESTROY = 0x0002
WM_QUIT = 0x0012
WM_USER = 0x0400
WM_TRAY = WM_USER + 20
WM_LBUTTONUP = 0x0202
WM_RBUTTONUP = 0x0205
NIM_ADD = 0x00000000
NIM_MODIFY = 0x00000001
NIM_DELETE = 0x00000002
NIM_SETVERSION = 0x00000004
NIF_MESSAGE = 0x00000001
NIF_ICON = 0x00000002
NIF_TIP = 0x00000004
NIF_INFO = 0x00000010
NOTIFYICON_VERSION_4 = 4
IMAGE_ICON = 1
LR_LOADFROMFILE = 0x0010
LR_DEFAULTSIZE = 0x0040
GWL_WNDPROC = -4
HWND_MESSAGE = ctypes.c_void_p(-3)
MF_STRING = 0x00000000
MF_SEPARATOR = 0x00000800
TPM_LEFTALIGN = 0x0000
TPM_RIGHTBUTTON = 0x0002
TPM_RETURNCMD = 0x0100
TRACK_POPUP = TPM_RIGHTBUTTON | TPM_RETURNCMD
CREATE_NO_WINDOW = 0x08000000

# PDH 常量
PDH_FMT_DOUBLE = 0x00000200
PDH_MORE_DATA = 0x800007D2
PDH_INVALID_HANDLE = 0xC0000BBB
ERROR_SUCCESS = 0

# 主题配色（深色）
C_BG = "#15181e"
C_CARD = "#1e222a"
C_CARD2 = "#252a34"
C_BORDER = "#2f3542"
C_FG = "#e8eaed"
C_SUB = "#8b93a1"
C_ACCENT = "#4c8dff"
C_GREEN = "#3ecf8e"
C_YELLOW = "#f5c451"
C_RED = "#f2645a"


def load_color(pct: float) -> str:
    if pct < 50:
        return C_GREEN
    if pct < 80:
        return C_YELLOW
    return C_RED


def short_gpu_name(name: str) -> str:
    """压缩显卡名以适配窄面板。"""
    n = name.strip()
    for p in ("NVIDIA GeForce ", "NVIDIA ", "AMD Radeon ", "Intel(R) "):
        if n.startswith(p):
            n = n[len(p):]
            break
    n = n.replace("(R)", "").replace("(TM)", "")
    return n if len(n) <= 30 else n[:29] + "…"


def hr_bytes(mb: float) -> str:
    if mb >= 1024:
        return "%.1f GB" % (mb / 1024.0)
    return "%.0f MB" % mb


# ---------------------------------------------------------------- Win32 封装

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_user32 = ctypes.WinDLL("user32", use_last_error=True)
_shell32 = ctypes.WinDLL("shell32", use_last_error=True)
_pdh = ctypes.WinDLL("pdh.dll", use_last_error=True)

# 高 DPI 感知，避免字体模糊
try:
    _shcore = ctypes.WinDLL("shcore", use_last_error=True)
    _shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        _user32.SetProcessDPIAware()
    except Exception:
        pass


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


def snapshot_processes() -> Dict[int, tuple]:
    """返回 {pid: (name, ppid)}，使用 Toolhelp 快照，速度快。"""
    out: Dict[int, tuple] = {}
    h = _kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not h or h == ctypes.c_void_p(-1).value:
        return out
    try:
        pe = PROCESSENTRY32W()
        pe.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = _kernel32.Process32FirstW(h, byref(pe))
        while ok:
            out[int(pe.th32ProcessID)] = (pe.szExeFile, int(pe.th32ParentProcessID))
            ok = _kernel32.Process32NextW(h, byref(pe))
    finally:
        _kernel32.CloseHandle(h)
    return out


def terminate_process(pid: int) -> str:
    """结束进程。返回错误消息；成功返回空字符串。"""
    if pid in PROTECTED_PIDS:
        return "系统核心进程（PID %d），已禁止结束" % pid
    h = _kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
    if not h:
        err = ctypes.get_last_error()
        if err == 5:
            return "拒绝访问：需要以管理员身份运行本面板"
        if err == 87:
            return "进程不存在（可能已退出）"
        return "无法打开进程（错误码 %d）" % err
    try:
        if not _kernel32.TerminateProcess(h, 1):
            return "TerminateProcess 失败（错误码 %d）" % ctypes.get_last_error()
    finally:
        _kernel32.CloseHandle(h)
    return ""


def child_pids(all_procs: Dict[int, tuple], root: int) -> List[int]:
    """找出 root 的所有后代 PID（BFS）。"""
    kids: List[int] = []
    frontier = [root]
    seen = {root}
    while frontier:
        nxt = []
        for pid, (_, ppid) in all_procs.items():
            if ppid in frontier and pid not in seen:
                seen.add(pid)
                kids.append(pid)
                nxt.append(pid)
        frontier = nxt
    return kids


# ---------------------------------------------------------------- PDH 采样器

class _U(ctypes.Union):
    _fields_ = [
        ("longValue", ctypes.c_long),
        ("doubleValue", ctypes.c_double),
        ("largeValue", ctypes.c_longlong),
        ("ansiStringValue", ctypes.c_char_p),
        ("wideStringValue", ctypes.c_wchar_p),
    ]


class PDH_FMT_COUNTERVALUE(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("CStatus", wintypes.DWORD), ("u", _U)]


class PDH_FMT_COUNTERVALUE_ITEM(ctypes.Structure):
    _fields_ = [("szName", ctypes.c_wchar_p), ("FmtValue", PDH_FMT_COUNTERVALUE)]


# 注意：PDH_STATUS 是 HRESULT 风格的无符号 32 位值，
# PDH_MORE_DATA = 0x800007D2 超出 c_long 上界，必须用 c_ulong 否则比较恒为假。
_pdh.PdhOpenQueryW.argtypes = [wintypes.LPCWSTR, ctypes.c_void_p, POINTER(wintypes.HANDLE)]
_pdh.PdhOpenQueryW.restype = ctypes.c_ulong
_pdh.PdhAddEnglishCounterW.argtypes = [wintypes.HANDLE, wintypes.LPCWSTR,
                                       ctypes.c_void_p, POINTER(wintypes.HANDLE)]
_pdh.PdhAddEnglishCounterW.restype = ctypes.c_ulong
_pdh.PdhCollectQueryData.argtypes = [wintypes.HANDLE]
_pdh.PdhCollectQueryData.restype = ctypes.c_ulong
_pdh.PdhCloseQuery.argtypes = [wintypes.HANDLE]
_pdh.PdhGetFormattedCounterArrayW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, POINTER(wintypes.DWORD),
    POINTER(wintypes.DWORD), POINTER(PDH_FMT_COUNTERVALUE_ITEM)]
_pdh.PdhGetFormattedCounterArrayW.restype = ctypes.c_ulong

# 消息循环相关函数的完整签名（避免 64 位参数溢出）
_user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                   wintypes.WPARAM, wintypes.LPARAM]
_user32.DefWindowProcW.restype = ctypes.c_longlong
_user32.CallWindowProcW.argtypes = [ctypes.c_void_p, wintypes.HWND, wintypes.UINT,
                                    wintypes.WPARAM, wintypes.LPARAM]
_user32.CallWindowProcW.restype = ctypes.c_longlong
_user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
_user32.SetWindowLongPtrW.restype = ctypes.c_void_p  # 64 位指针，绝不能用默认 int 截断

# 实例名示例：pid_10708_luid_0x00000000_0x0000D679_phys_0_eng_0_engtype_3D
RE_ENGINE = re.compile(
    r"pid_(\d+)_luid_0x[0-9a-fA-F]+_0x([0-9a-fA-F]+)_phys_(\d+)_eng_(\d+)_engtype_(.+)$")
RE_PROCMEM = re.compile(r"pid_(\d+)_luid_0x[0-9a-fA-F]+_0x([0-9a-fA-F]+)_.*$")
# 示例：luid_0x00000000_0x0000D679_phys_0_part_0
RE_ADAPTER = re.compile(r"luid_0x[0-9a-fA-F]+_0x([0-9a-fA-F]+)_phys_(\d+)")

REG_GPU_CLASS = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"


def enum_gpu_registry() -> List[dict]:
    """从驱动注册表读取显卡型号与总显存（nvidia-smi 不可用时的兜底）。"""
    out: List[dict] = []
    try:
        root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, REG_GPU_CLASS)
    except OSError:
        return out
    try:
        i = 0
        while True:
            try:
                sub = winreg.EnumKey(root, i)
            except OSError:
                break
            i += 1
            if not sub.isdigit():
                continue
            try:
                k = winreg.OpenKey(root, sub)
                try:
                    desc, _ = winreg.QueryValueEx(k, "DriverDesc")
                except OSError:
                    winreg.CloseKey(k)
                    continue
                total = 0
                for vn in ("HardwareInformation.qwMemorySize",
                           "HardwareInformation.MemorySize"):
                    try:
                        v, _ = winreg.QueryValueEx(k, vn)
                        if v:
                            # 部分驱动把显存存成 REG_BINARY，需按小端字节序解析
                            total = (int.from_bytes(v, "little")
                                     if isinstance(v, (bytes, bytearray)) else int(v))
                            break
                    except (OSError, ValueError, TypeError):
                        pass
                winreg.CloseKey(k)
                if total and total > 64 * 1024 * 1024:  # 过滤掉 0 显存的虚拟设备
                    out.append({"name": desc, "total_mb": total / (1024.0 * 1024.0)})
            except OSError:
                continue
    finally:
        winreg.CloseKey(root)
    return out


class PdhSampler:
    """Windows GPU 性能计数器采样（每进程 GPU 引擎利用率 / 显存）。"""

    def __init__(self):
        self.query = wintypes.HANDLE()
        self.ok = False
        self.h_util = wintypes.HANDLE()
        self.h_mem = wintypes.HANDLE()
        self.h_local = wintypes.HANDLE()
        self.h_adapter = wintypes.HANDLE()
        self.primed = False
        self._last = 0.0
        # PDH 查询句柄非线程安全：采集线程与"强制刷新"线程可能并发调用
        self._lock = threading.Lock()
        if _pdh.PdhOpenQueryW(None, None, byref(self.query)) != ERROR_SUCCESS:
            return
        # 计数器可能在某些系统上不存在，逐个尝试
        self.h_util = self._add(r"\GPU Engine(*)\Utilization Percentage")
        self.h_mem = self._add(r"\GPU Process Memory(*)\Dedicated Usage")
        self.h_local = self._add(r"\GPU Local Adapter Memory(*)\Local Usage")
        self.h_adapter = self._add(r"\GPU Adapter Memory(*)\Dedicated Usage")
        self.ok = bool(self.h_util or self.h_mem or self.h_local or self.h_adapter)
        if self.ok:
            _pdh.PdhCollectQueryData(self.query)  # 首次采集打基线
            self._last = time.time()

    def _add(self, path: str) -> Optional[wintypes.HANDLE]:
        h = wintypes.HANDLE()
        rc = _pdh.PdhAddEnglishCounterW(self.query, path, None, byref(h))
        return h if rc == ERROR_SUCCESS else None

    def _array(self, h) -> List[tuple]:
        """取回 (实例名, 数值) 列表。"""
        if not h:
            return []
        size = wintypes.DWORD(0)
        count = wintypes.DWORD(0)
        rc = _pdh.PdhGetFormattedCounterArrayW(h, PDH_FMT_DOUBLE, byref(size),
                                               byref(count), None)
        if rc != PDH_MORE_DATA or size.value == 0:
            # 数据尚未就绪（仅采集过一次、无差值）时 PDH 会拒绝，静默跳过
            return []
        buf = (ctypes.c_byte * size.value)()
        rc = _pdh.PdhGetFormattedCounterArrayW(h, PDH_FMT_DOUBLE, byref(size),
                                               byref(count), ctypes.cast(buf, POINTER(
                                                   PDH_FMT_COUNTERVALUE_ITEM)))
        if rc != ERROR_SUCCESS:
            return []
        items = ctypes.cast(buf, POINTER(PDH_FMT_COUNTERVALUE_ITEM))
        out = []
        for i in range(count.value):
            it = items[i]
            if it.FmtValue.CStatus != ERROR_SUCCESS:
                continue
            out.append((it.szName or "", it.FmtValue.doubleValue))
        return out

    def sample(self) -> Dict[str, object]:
        """
        返回 {
          'per_pid': {pid: {'util': float%, 'mem_mb': float}},
          'per_gpu': {luid: {'util': float%, 'mem_mb': float}},
          'total_util': float
        }
        """
        with self._lock:
            return self._sample()

    def _sample(self) -> Dict[str, object]:
        res = {"per_pid": {}, "per_gpu": {}, "total_util": 0.0}
        if not self.ok:
            return res
        if not self.primed:
            # 利用率类计数器依赖两次采样的差值：若距基线太近，先补足窗口
            gap = time.time() - self._last
            if gap < 0.5:
                time.sleep(0.5 - gap)
        _pdh.PdhCollectQueryData(self.query)
        self._last = time.time()
        self.primed = True

        # 每 PID / 每 GPU 的引擎利用率：按 (pid, luid) 求和后 clamp 100
        pid_luid_util: Dict[tuple, float] = {}
        for name, val in self._array(self.h_util):
            m = RE_ENGINE.search(name)
            if not m or val <= 0:
                continue
            pid = int(m.group(1))
            luid = m.group(2).upper()
            key = (pid, luid)
            pid_luid_util[key] = pid_luid_util.get(key, 0.0) + val

        per_pid: Dict[int, Dict[str, float]] = {}
        per_gpu: Dict[str, float] = {}
        for (pid, luid), v in pid_luid_util.items():
            v = min(v, 100.0)
            per_gpu[luid] = min(per_gpu.get(luid, 0.0) + v, 100.0)
            cur = per_pid.setdefault(pid, {"util": 0.0, "mem_mb": 0.0})
            cur["util"] = max(cur["util"], v)  # 跨 GPU 取最大

        # 显存（GiB/MiB 单位由计数器决定：Dedicated Usage 单位为 Bytes）
        for name, val in self._array(self.h_mem):
            m = RE_PROCMEM.search(name)
            if not m or val <= 0:
                continue
            pid = int(m.group(1))
            cur = per_pid.setdefault(pid, {"util": 0.0, "mem_mb": 0.0})
            cur["mem_mb"] = max(cur["mem_mb"], val / (1024.0 * 1024.0))

        # 整卡已用显存：优先 Local Usage（专用显存口径，与 nvidia-smi 一致）
        gpus = {l: {"util": u, "mem_mb": 0.0} for l, u in per_gpu.items()}
        for h, keyed_by_pid in ((self.h_local, False), (self.h_adapter, False)):
            if not h:
                continue
            for name, val in self._array(h):
                m = RE_ADAPTER.search(name)
                if not m or val <= 0:
                    continue
                luid = m.group(1).upper()
                if luid not in gpus:
                    gpus[luid] = {"util": 0.0, "mem_mb": 0.0}
                mb = val / (1024.0 * 1024.0)
                cur = gpus[luid]["mem_mb"]
                # 已有值且来自 Local Usage 时不覆盖
                if cur == 0.0 or h is self.h_local:
                    gpus[luid]["mem_mb"] = mb if h is self.h_local else max(cur, mb)

        res["per_pid"] = per_pid
        res["per_gpu"] = gpus
        res["total_util"] = max(per_gpu.values()) if per_gpu else 0.0
        return res

    def close(self):
        if self.ok:
            _pdh.PdhCloseQuery(self.query)
            self.ok = False


# ---------------------------------------------------------------- nvidia-smi

GPU_FIELDS = ["index", "name", "utilization.gpu", "memory.used", "memory.total",
              "temperature.gpu", "power.draw", "fan.speed"]
GPU_QUERY = ",".join(GPU_FIELDS)


def find_nvidia_smi() -> Optional[str]:
    root = os.environ.get("SystemRoot", r"C:\Windows")
    for p in (os.path.join(root, "System32", "nvidia-smi.exe"),
              os.path.join(root, "SysWOW64", "nvidia-smi.exe")):
        if os.path.exists(p):
            return p
    return shutil.which("nvidia-smi")


def run_nvidia_smi(args: List[str], timeout: float = 4.0) -> Optional[str]:
    exe = find_nvidia_smi()
    if not exe:
        return None
    try:
        r = subprocess.run([exe] + args, capture_output=True, text=True,
                           timeout=timeout, creationflags=CREATE_NO_WINDOW)
        out = (r.stdout or "").strip()
    except Exception:
        return None
    if not out or "Failed to initialize" in out or "NVML" in out and "Error" in out:
        return None
    return out


def _num(s: str) -> Optional[float]:
    s = (s or "").strip()
    if not s or s in ("[N/A]", "[Not Supported]", "N/A", "Unknown"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def query_gpus() -> List[dict]:
    """返回 GPU 列表；失败返回空列表。"""
    out = run_nvidia_smi(["--query-gpu=" + GPU_QUERY, "--format=csv,noheader,nounits"])
    if not out:
        return []
    gpus = []
    for line in out.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < len(GPU_FIELDS):
            # name 里可能带逗号，从后往前补
            parts = [p.strip() for p in line.rsplit(",", len(GPU_FIELDS) - 1)]
        if len(parts) != len(GPU_FIELDS):
            continue
        gpus.append({
            "index": _num(parts[0]) or 0,
            "name": parts[1],
            "util": _num(parts[2]) or 0.0,
            "mem_used": _num(parts[3]) or 0.0,
            "mem_total": _num(parts[4]) or 0.0,
            "temp": _num(parts[5]),
            "power": _num(parts[6]),
            "fan": _num(parts[7]),
            "source": "nvidia-smi",
        })
    return gpus


def query_gpu_procs() -> Dict[int, float]:
    """返回 {pid: 显存 MiB}；失败返回 {}。"""
    out = run_nvidia_smi(["--query-compute-apps=pid,used_memory",
                          "--format=csv,noheader,nounits"])
    if not out:
        return {}
    res: Dict[int, float] = {}
    for line in out.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 2:
            continue
        pid = _num(parts[0])
        mem = _num(parts[1])
        if pid is None:
            continue
        res[int(pid)] = max(res.get(int(pid), 0.0), mem or 0.0)
    return res


# ---------------------------------------------------------------- 数据模型

@dataclass
class GpuStat:
    key: str
    name: str
    util: float = 0.0
    mem_used: float = 0.0
    mem_total: float = 0.0
    temp: Optional[float] = None
    power: Optional[float] = None
    fan: Optional[float] = None
    source: str = "pdh"


@dataclass
class GpuProc:
    pid: int
    name: str
    util: float = 0.0
    mem_mb: float = 0.0


@dataclass
class Snapshot:
    gpus: List[GpuStat] = field(default_factory=list)
    procs: List[GpuProc] = field(default_factory=list)
    source: str = "-"
    ts: float = 0.0


class GpuCollector(threading.Thread):
    """后台采集线程：nvidia-smi 优先，PDH 兜底补充每进程利用率。"""

    def __init__(self, out_queue: "queue.Queue"):
        super().__init__(daemon=True)
        self.q = out_queue
        self.pdh = PdhSampler()
        self.interval = 1.5
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        while not self._stop.is_set():
            try:
                snap = self.collect()
                try:
                    self.q.put_nowait(snap)
                except queue.Full:
                    # 界面暂时消费不动时丢掉最旧一帧，避免采集线程被堵死
                    try:
                        self.q.get_nowait()
                    except Exception:
                        pass
                    try:
                        self.q.put_nowait(snap)
                    except Exception:
                        pass
            except Exception as e:  # 采集异常不能中断线程
                try:
                    self.q.put(Snapshot(source="采集异常: %s" % e))
                except Exception:
                    pass
            self._stop.wait(self.interval)

    def collect(self) -> Snapshot:
        procs_map = snapshot_processes()
        smi_gpus = query_gpus()
        smi_mem = query_gpu_procs()
        pdh = self.pdh.sample()

        gpus: List[GpuStat] = []
        source = "nvidia-smi"
        if smi_gpus:
            # 单卡时若驱动未报告总显存，用注册表值补齐
            if len(smi_gpus) == 1 and smi_gpus[0]["mem_total"] <= 0:
                reg = enum_gpu_registry()
                if reg:
                    smi_gpus[0]["mem_total"] = reg[0]["total_mb"]
            for g in smi_gpus:
                gpus.append(GpuStat(
                    key="smi%d" % g["index"], name=g["name"], util=g["util"],
                    mem_used=g["mem_used"], mem_total=g["mem_total"],
                    temp=g["temp"], power=g["power"], fan=g["fan"],
                    source="nvidia-smi"))
        else:
            source = "性能计数器"
            reg = enum_gpu_registry()
            # 只保留有活动或确实占着显存的 GPU，避免把空闲的核显也列出来
            active = [(l, d) for l, d in pdh["per_gpu"].items()
                      if d["mem_mb"] >= 1.0 or d["util"] >= 0.5]
            if not active:
                active = list(pdh["per_gpu"].items())
            active.sort(key=lambda kv: -kv[1]["mem_mb"])
            reg.sort(key=lambda r: -r["total_mb"])
            # 数量一致时才按序配对型号/总显存，避免多卡混插时错位
            pairable = len(reg) == len(active)
            for i, (luid, d) in enumerate(active):
                nm, tot = "GPU (LUID 0x%s)" % luid, 0.0
                if pairable:
                    nm, tot = reg[i]["name"], reg[i]["total_mb"]
                gpus.append(GpuStat(key="luid" + luid, name=nm, util=d["util"],
                                    mem_used=d["mem_mb"], mem_total=tot,
                                    source="pdh"))

        # 进程合并：PDH 利用率 + nvidia-smi 显存
        merged: Dict[int, GpuProc] = {}
        for pid, d in pdh["per_pid"].items():
            merged[pid] = GpuProc(pid=pid, name="", util=d["util"], mem_mb=d["mem_mb"])
        for pid, mb in smi_mem.items():
            if pid in merged:
                merged[pid].mem_mb = max(merged[pid].mem_mb, mb)
            else:
                merged[pid] = GpuProc(pid=pid, name="", util=0.0, mem_mb=mb)

        out = []
        for pid, p in merged.items():
            name = procs_map.get(pid, ("", 0))[0] or "<已退出>"
            p.name = name
            out.append(p)
        # 有 GPU 活动或占用显存的优先，再按显存降序
        out.sort(key=lambda p: (-p.mem_mb, -p.util, p.pid))

        return Snapshot(gpus=gpus, procs=out, source=source, ts=time.time())


# ---------------------------------------------------------------- 托盘图标

def make_tray_icon(util: float, path: str = ICON_PATH) -> Optional[str]:
    """生成随负载变色的托盘图标，返回文件路径；失败返回 None。"""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except Exception:
        return None
    S = 64
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    col = load_color(util)
    rgb = tuple(int(col[i:i + 2], 16) for i in (1, 3, 5))
    d.ellipse([2, 2, S - 3, S - 3], fill=rgb + (255,), outline=(255, 255, 255, 90), width=3)
    try:
        f = ImageFont.truetype(r"C:\Windows\Fonts\segoeuib.ttf", 30)
    except Exception:
        f = ImageFont.load_default()
    d.text((S / 2, S / 2 + 1), "G", font=f, fill=(16, 18, 22, 255), anchor="mm")
    tmp = path + ".tmp"
    try:
        img.save(tmp, format="ICO", sizes=[(16, 16), (32, 32), (48, 48)])
        os.replace(tmp, path)
    except Exception:
        return None
    return path


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", ctypes.c_byte * 16),
        ("hBalloonIcon", wintypes.HICON),
    ]


class TrayIcon:
    """系统托盘图标（纯 ctypes，无需 pystray）。"""

    def __init__(self, hwnd: int, tip: str = APP_NAME):
        self.hwnd = wintypes.HWND(hwnd)
        self.uid = 99
        self.msg = WM_TRAY
        self.icon = None
        self.added = False
        self.tip = tip
        self._icon_col = None  # 上次生成的图标颜色档位（颜色没变就复用 HICON）

    def _fill(self):
        n = NOTIFYICONDATAW()
        n.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        n.hWnd = self.hwnd
        n.uID = self.uid
        n.uCallbackMessage = self.msg
        n.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        if self.icon:
            n.hIcon = self.icon
        n.szTip = self.tip[:127]
        return n

    def show(self, util: float = 0.0):
        col = load_color(util)
        if col != self._icon_col:  # 颜色档位没变就不重画/重载图标
            p = make_tray_icon(util)
            if p:
                h = _user32.LoadImageW(None, p, IMAGE_ICON, 0, 0,
                                       LR_LOADFROMFILE | LR_DEFAULTSIZE)
                if h:
                    if self.icon:
                        _user32.DestroyIcon(self.icon)
                    self.icon = wintypes.HICON(h)
                    self._icon_col = col
        n = self._fill()
        if not self.added:
            _shell32.Shell_NotifyIconW(NIM_ADD, byref(n))
            _shell32.Shell_NotifyIconW(NIM_SETVERSION, byref(n))
            self.added = True
        else:
            _shell32.Shell_NotifyIconW(NIM_MODIFY, byref(n))

    def update(self, util: float, tip: str):
        self.tip = tip
        self.show(util)

    def remove(self):
        if self.added:
            n = self._fill()
            _shell32.Shell_NotifyIconW(NIM_DELETE, byref(n))
            self.added = False
        if self.icon:
            _user32.DestroyIcon(self.icon)
            self.icon = None


def popup_menu(hwnd: int, items: List[tuple]) -> int:
    """在鼠标位置弹出菜单，items = [(id, 文本)]，文本为 None 表示分隔符。返回选中 id。"""
    hmenu = _user32.CreatePopupMenu()
    for mid, text in items:
        if text is None:
            _user32.AppendMenuW(hmenu, MF_SEPARATOR, 0, None)
        else:
            _user32.AppendMenuW(hmenu, MF_STRING, mid, text)
    pt = wintypes.POINT()
    _user32.GetCursorPos(byref(pt))
    _user32.SetForegroundWindow(wintypes.HWND(hwnd))
    cmd = _user32.TrackPopupMenu(hmenu, TRACK_POPUP, pt.x, pt.y, 0,
                                 wintypes.HWND(hwnd), None)
    _user32.PostMessageW(wintypes.HWND(hwnd), 0, 0, 0)  # 关闭菜单
    _user32.DestroyMenu(hmenu)
    return cmd or 0


# ---------------------------------------------------------------- 全局热键

WNDPROCTYPE = ctypes.WINFUNCTYPE(ctypes.c_longlong, wintypes.HWND, wintypes.UINT,
                                 wintypes.WPARAM, wintypes.LPARAM)


class HotkeyThread(threading.Thread):
    """注册 Ctrl+Alt+G 全局热键，独立消息循环（message-only window）。"""

    def __init__(self, callback):
        super().__init__(daemon=True)
        self.cb = callback
        self.hwnd = None
        self._proc = None

    def run(self):
        try:
            hinst = _kernel32.GetModuleHandleW(None)

            class WNDCLASSEXW(ctypes.Structure):
                _fields_ = [("cbSize", wintypes.UINT), ("style", wintypes.UINT),
                            ("lpfnWndProc", WNDPROCTYPE), ("cbClsExtra", ctypes.c_int),
                            ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE),
                            ("hIcon", wintypes.HICON), ("hCursor", wintypes.HICON),
                            ("hbrBackground", wintypes.HBRUSH),
                            ("lpszMenuName", wintypes.LPCWSTR),
                            ("lpszClassName", wintypes.LPCWSTR), ("hIconSm", wintypes.HICON)]

            self._proc = WNDPROCTYPE(self._wndproc)
            wc = WNDCLASSEXW()
            wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
            wc.lpfnWndProc = self._proc
            wc.hInstance = wintypes.HINSTANCE(hinst)
            wc.lpszClassName = "GpuMonHotkeyCls"
            if not _user32.RegisterClassExW(byref(wc)):
                return
            hwnd = _user32.CreateWindowExW(
                0, "GpuMonHotkeyCls", "gpumon", 0, 0, 0, 0, 0,
                HWND_MESSAGE, None, wintypes.HINSTANCE(hinst), None)
            if not hwnd:
                return
            self.hwnd = hwnd
            MOD_CTRL, MOD_ALT, MOD_NOREPEAT = 0x0002, 0x0001, 0x4000
            if not _user32.RegisterHotKey(wintypes.HWND(hwnd), 1,
                                          MOD_CTRL | MOD_ALT | MOD_NOREPEAT, 0x47):
                return
            msg = wintypes.MSG()
            while _user32.GetMessageW(byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY:
                    try:
                        self.cb()
                    except Exception:
                        pass
                _user32.TranslateMessage(byref(msg))
                _user32.DispatchMessageW(byref(msg))
        except Exception:
            pass

    def _wndproc(self, hwnd, msg, wparam, lparam):
        if msg == WM_DESTROY:
            _user32.PostQuitMessage(0)
            return 0
        return _user32.DefWindowProcW(wintypes.HWND(hwnd), msg, wparam, lparam)

    def stop(self):
        if self.hwnd:
            _user32.PostMessageW(wintypes.HWND(self.hwnd), WM_DESTROY, 0, 0)


# ---------------------------------------------------------------- GUI

class Bar(tk.Canvas):
    """扁平进度条。"""

    def __init__(self, master, height=8, **kw):
        super().__init__(master, height=height, bg=C_CARD, highlightthickness=0, **kw)
        self.h = height

    def set(self, pct: float, color: str = None):
        self.delete("all")
        w = self.winfo_width()
        if w <= 1:
            w = int(self.cget("width") or 200)
        pct = max(0.0, min(100.0, pct))
        col = color or load_color(pct)
        self.create_rectangle(0, 0, w, self.h, fill=C_CARD2, outline="")
        fw = max(2, int(w * pct / 100.0))
        self.create_rectangle(0, 0, fw, self.h, fill=col, outline="")


class Spark(tk.Canvas):
    """60 点滚动折线图。"""

    def __init__(self, master, n=60, **kw):
        super().__init__(master, height=46, bg=C_CARD, highlightthickness=0, **kw)
        self.n = n
        self.data = [0.0] * n
        self.bind("<Configure>", lambda e: self.draw())

    def push(self, v: float):
        self.data.append(float(v))
        if len(self.data) > self.n:
            del self.data[0]
        self.draw()

    def draw(self):
        self.delete("all")
        w = self.winfo_width() or 1
        h = self.winfo_height() or 1
        pad = 3
        # 网格
        for frac in (0.25, 0.5, 0.75):
            y = h - pad - (h - 2 * pad) * frac
            self.create_line(0, y, w, y, fill="#2a2f3a")
        n = len(self.data)
        if n < 2:
            return
        step = w / float(self.n - 1)
        pts = []
        for i, v in enumerate(self.data):
            x = i * step
            y = h - pad - (h - 2 * pad) * (max(0.0, min(100.0, v)) / 100.0)
            pts.append((x, y))
        poly = list(pts) + [(pts[-1][0], h), (pts[0][0], h)]
        self.create_polygon(poly, fill="#1f3a5f", outline="")
        self.create_line([c for p in pts for c in p], fill=C_ACCENT, width=1.6,
                         smooth=False)
        lx, ly = pts[-1]
        self.create_oval(lx - 2.5, ly - 2.5, lx + 2.5, ly + 2.5,
                         fill=load_color(self.data[-1]), outline="")


class GPUPanel(tk.Tk):
    W, H = DEFAULT_W, DEFAULT_H

    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.configure(bg=C_BG)
        self.overrideredirect(True)

        self.cfg = self.load_cfg()
        # 窗口尺寸记忆（右下角可拖动缩放）
        self.W = max(MIN_W, int(self.cfg.get("w", DEFAULT_W)))
        self.H = max(MIN_H, int(self.cfg.get("h", DEFAULT_H)))
        self.adv_w = max(ADV_MIN_W, int(self.cfg.get("adv_w", ADV_DEFAULT_W)))
        self.adv_h = max(ADV_MIN_H, int(self.cfg.get("adv_h", ADV_DEFAULT_H)))

        # 上次的置顶/透明度/刷新率设置要在启动时就生效（之前只存不用）
        self.attributes("-topmost", bool(self.cfg.get("topmost", True)))
        self.alpha = float(self.cfg.get("alpha", 0.96))
        self.attributes("-alpha", self.alpha)

        self.q: "queue.Queue[Snapshot]" = queue.Queue(maxsize=8)
        self.collector = GpuCollector(self.q)
        self.collector.interval = max(0.5, min(10.0,
                                               self.cfg.get("interval", 1500) / 1000.0))
        self.snap: Optional[Snapshot] = None
        self.mini = False            # 悬浮球模式
        self.hidden = False          # 完全隐藏（仅托盘）
        self.last_icon_util = -1.0
        self.proc_rows: Dict[str, GpuProc] = {}

        self._build()
        # 高 DPI 下按钮的像素需求变大：按实际请求宽度推算最小宽度，
        # 否则窄窗口里"结束进程 / 模型顾问"会被裁掉
        need = (self.btn_llm.winfo_reqwidth() + self.btn_kill.winfo_reqwidth()
                + self.btn_settings.winfo_reqwidth()
                + self.btn_advisor.winfo_reqwidth() + 24)
        self._min_w = max(MIN_W, need)
        self.W = max(self.W, self._min_w)
        self._place_initial()
        self.bind("<Configure>", self._on_main_configure)
        self._setup_tray()
        self._start_hotkey()

        self.collector.start()
        self.after(300, self._poll)
        self.protocol("WM_DELETE_WINDOW", self.quit_app)

        # 后台预热模型顾问的硬件探测（避免点击按钮时卡 1~3 秒）
        def _warm_done():
            # 探测完成时若顾问窗口正开着且显示的还是默认值，自动刷新
            if self.advisor_win.winfo_viewable() and self._advisor_key == self.advisor_device:
                try:
                    self._render_advisor()
                except Exception:
                    pass
        try:
            if BASE_DIR not in sys.path:
                sys.path.insert(0, BASE_DIR)
            import model_advisor as _MA
            _MA.warm_local_config(lambda: self.after(0, _warm_done))
        except Exception:
            pass

    # ------------------------------------------------------------ 配置

    def load_cfg(self) -> dict:
        d = {"x": None, "y": None, "alpha": 0.96, "interval": 1500, "topmost": True,
             "w": DEFAULT_W, "h": DEFAULT_H,
             "adv_w": ADV_DEFAULT_W, "adv_h": ADV_DEFAULT_H}
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                d.update(json.load(f))
        except Exception:
            pass
        return d

    def save_cfg(self):
        try:
            # 悬浮球/隐藏状态下 winfo 尺寸不是面板尺寸，不能覆盖记忆值
            if not self.mini and self.winfo_width() > 100:
                self.W, self.H = self.winfo_width(), self.winfo_height()
            try:
                aw, ah = self.advisor_win.winfo_width(), self.advisor_win.winfo_height()
                if aw > 100 and ah > 100:
                    self.adv_w, self.adv_h = aw, ah
            except Exception:
                pass
            self.cfg.update({"x": self.winfo_x(), "y": self.winfo_y(),
                             "alpha": self.alpha,
                             # 隐藏到托盘时 collector.interval 被临时降到 5s，
                             # 不能把它当作用户设置写回配置
                             "interval": int(self.cfg.get("interval", 1500)),
                             "topmost": bool(self.attributes("-topmost")),
                             "w": int(self.W), "h": int(self.H),
                             "adv_w": int(self.adv_w), "adv_h": int(self.adv_h)})
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(self.cfg, f)
        except Exception:
            pass

    def _place_initial(self):
        x, y = self.cfg.get("x"), self.cfg.get("y")
        if x is None or y is None:
            x = self.winfo_screenwidth() - self.W - 14
            y = 70
        # 钳制到屏幕内：换显示器/改分辨率后旧坐标可能把面板整个丢出可视区
        x = max(-self.W + 60, min(int(x), self.winfo_screenwidth() - 60))
        y = max(0, min(int(y), self.winfo_screenheight() - 60))
        self.geometry("%dx%d+%d+%d" % (self.W, self.H, x, y))

    # ------------------------------------------------------------ 界面

    def _build(self):
        # 标题栏
        self.bar = tk.Frame(self, bg=C_CARD, height=30)
        self.bar.pack(fill="x")
        self.bar.pack_propagate(False)
        tk.Label(self.bar, text="◆", fg=C_ACCENT, bg=C_CARD,
                 font=(FONT, 10)).pack(side="left", padx=(9, 4))
        self.lbl_title = tk.Label(self.bar, text=APP_NAME, bg=C_CARD, fg=C_FG,
                                  font=(FONT, 9, "bold"))
        self.lbl_title.pack(side="left")
        self.lbl_src = tk.Label(self.bar, text="", bg=C_CARD, fg=C_SUB, font=(FONT, 7))
        self.lbl_src.pack(side="left", padx=(6, 0))

        for txt, cmd, col in (("✕", self.quit_app, C_RED),
                              ("—", self.toggle_hide, C_SUB),
                              ("◎", self.toggle_mini, C_SUB),
                              ("↻", self.force_refresh, C_SUB)):
            b = tk.Label(self.bar, text=txt, bg=C_CARD, fg=col,
                         font=(FONT, 10), cursor="hand2", width=2)
            b.pack(side="right")
            b.bind("<Button-1>", lambda e, c=cmd: c())

        for w in (self.bar, self.lbl_title, self.lbl_src):
            w.bind("<ButtonPress-1>", self._drag_start)
            w.bind("<B1-Motion>", self._drag_move)
            w.bind("<Double-Button-1>", lambda e: self.toggle_mini())

        # 主体
        self.body = tk.Frame(self, bg=C_BG)
        self.body.pack(fill="both", expand=True, padx=10, pady=(8, 0))

        self.gpu_frame = tk.Frame(self.body, bg=C_BG)
        self.gpu_frame.pack(fill="x")
        self.gpu_cards: Dict[str, dict] = {}

        # 图表
        head = tk.Frame(self.body, bg=C_BG)
        head.pack(fill="x", pady=(10, 2))
        tk.Label(head, text="利用率 (近 90 秒)", bg=C_BG, fg=C_SUB,
                 font=(FONT, 8)).pack(side="left")
        self.spark = Spark(self.body)
        self.spark.pack(fill="x")

        # 进程区
        phead = tk.Frame(self.body, bg=C_BG)
        phead.pack(fill="x", pady=(10, 3))
        self.lbl_pcount = tk.Label(phead, text="GPU 进程", bg=C_BG, fg=C_SUB,
                                   font=(FONT, 8))
        self.lbl_pcount.pack(side="left")
        tk.Label(phead, text="勾选 → 结束", bg=C_BG, fg="#5c6370",
                 font=(FONT, 7)).pack(side="right")

        wrap = tk.Frame(self.body, bg=C_BORDER)
        btns = tk.Frame(self.body, bg=C_BG)

        # 底部状态条（含缩放抓手）：必须最先 pack 到底部，才会贴在窗口最下沿
        self.statusbar = tk.Frame(self.body, bg=C_CARD, height=18)
        self.statusbar.pack(side="bottom", fill="x", pady=(6, 0))
        self.statusbar.pack_propagate(False)
        self.lbl_status = tk.Label(self.statusbar, text="就绪", bg=C_CARD, fg=C_SUB,
                                   font=(FONT, 7), anchor="w")
        self.lbl_status.pack(side="left", padx=(6, 0))
        self.grip = tk.Label(self.statusbar, text="◢", bg=C_CARD, fg="#5c6370",
                             font=(FONT, 8), cursor="size_nw_se")
        self.grip.pack(side="right", padx=(0, 3))
        self.grip.bind("<ButtonPress-1>", self._resize_start)
        self.grip.bind("<B1-Motion>", self._resize_move)
        self.grip.bind("<Double-Button-1>", lambda e: self._reset_size())

        btns.pack(side="bottom", fill="x", pady=(8, 6))  # 先占住底部，防止被表格挤出窗口
        wrap.pack(fill="both", expand=True)
        inner = tk.Frame(wrap, bg=C_CARD)
        inner.pack(fill="both", expand=True, padx=1, pady=1)

        st = ttk.Style()
        st.theme_use("clam")
        st.configure("T.Treeview", background=C_CARD, fieldbackground=C_CARD,
                     foreground=C_FG, borderwidth=0, rowheight=21)
        st.configure("T.Treeview.Heading", background=C_CARD2, foreground=C_SUB,
                     borderwidth=0, font=(FONT, 8))
        st.map("T.Treeview", background=[("selected", "#2d4a7a")],
               foreground=[("selected", "#ffffff")])
        # 滚动条跟随深色主题（clam 默认箭头是白底黑三角，很突兀）
        st.configure("TScrollbar", background=C_CARD2, troughcolor=C_BG,
                     bordercolor=C_BORDER, arrowcolor=C_SUB,
                     lightcolor=C_CARD2, darkcolor=C_CARD2, relief="flat")
        st.map("TScrollbar", background=[("active", C_BORDER)],
               arrowcolor=[("active", C_FG)])

        self.tree = ttk.Treeview(inner, style="T.Treeview", show="headings",
                                 columns=("name", "pid", "mem", "util"), height=7)
        # 表头点击排序（显存默认降序：最占显存的排最前）
        self.sort_col = "mem"
        self.sort_desc = True
        self._update_headings()
        self.tree.column("name", width=132, anchor="w")
        self.tree.column("pid", width=52, anchor="center")
        self.tree.column("mem", width=64, anchor="e")
        self.tree.column("util", width=46, anchor="e")
        self.tree.tag_configure("sys", foreground="#7a8290")
        self.tree.tag_configure("hot", foreground=C_RED)
        sb = ttk.Scrollbar(inner, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", lambda e: self.kill_selected())
        self.tree.bind("<Button-3>", self._row_menu)

        # 底部按钮
        self.btn_llm = tk.Button(
            btns, text="🚀 大模型", command=self.llm_mode,
            bg="#22303a", fg="#7fe3a0", activebackground="#2a3c48",
            activeforeground="#b0f0c8", relief="flat", bd=0,
            font=(FONT, 9, "bold"), cursor="hand2", height=1, pady=5)
        self.btn_kill = tk.Button(
            btns, text="结束进程", command=self.kill_selected,
            bg="#3a2226", fg="#ff9a92", activebackground="#4a2a2f",
            activeforeground="#ffd0cc", relief="flat", bd=0,
            font=(FONT, 9, "bold"), cursor="hand2", height=1, pady=5)
        self.btn_advisor = tk.Button(
            btns, text="📊 模型顾问", command=self.show_advisor,
            bg=C_CARD, fg="#6fb1d9", activebackground="#2a3a4a",
            activeforeground="#a0d4f0", relief="flat", bd=0,
            font=(FONT, 9), cursor="hand2", padx=6)
        self.btn_settings = tk.Button(
            btns, text="设置", command=self.show_settings,
            bg=C_CARD, fg=C_SUB, activebackground=C_CARD2,
            activeforeground=C_FG, relief="flat", bd=0,
            font=(FONT, 9), cursor="hand2", padx=6)

        # 先 pack 右侧固定按钮：中间带 expand 的按钮若先 pack，会先吃掉整行空间，
        # 把右侧按钮挤出窗口（旧版窄窗口下按钮被裁掉就是这个原因）
        self.btn_advisor.pack(side="right", padx=(0, 5))
        self.btn_settings.pack(side="right")
        self.btn_llm.pack(side="left", padx=(0, 5))
        self.btn_kill.pack(side="left", fill="x", expand=True)

        # ── 模型顾问面板 (初始隐藏) ────────────────────────────────
        self.advisor_win = tk.Toplevel(self)
        self.advisor_win.title("模型顾问")
        self.advisor_win.configure(bg=C_BG)
        self.advisor_win.overrideredirect(True)
        self.advisor_win.attributes("-topmost", True)
        self.advisor_win.withdraw()  # 初始隐藏
        self.advisor_device = "local"  # 当前推荐目标设备
        self._advisor_key = None       # 已渲染的设备 key（命中则秒开）
        self._adv_saved_geo = None     # 最大化前尺寸
        self._adv_feasible_only = False  # 只看能跑（隐藏装不下的）
        self._adv_rows = []            # 当前表格数据（供复制）
        self._adv_min_w = ADV_MIN_W    # 按内容算出的最小宽度（渲染后更新）

        # 标题栏（绑定拖动）
        a_head = tk.Frame(self.advisor_win, bg=C_CARD, height=36)
        a_head.pack(fill="x")
        a_head.pack_propagate(False)
        a_title = tk.Label(a_head, text="◆ 模型顾问", fg=C_ACCENT, bg=C_CARD,
                           font=(FONT, 10, "bold"))
        a_title.pack(side="left", padx=9)
        lbl_close = tk.Label(a_head, text="✕", fg=C_RED, bg=C_CARD,
                             font=(FONT, 10), cursor="hand2")
        lbl_close.pack(side="right", padx=9)
        lbl_max = tk.Label(a_head, text="□", fg=C_SUB, bg=C_CARD,
                           font=(FONT, 11), cursor="hand2")
        lbl_max.pack(side="right", padx=4)
        lbl_close.bind("<Button-1>", lambda e: self.advisor_win.withdraw())
        lbl_max.bind("<Button-1>", lambda e: self._advisor_maxtoggle(lbl_max))
        # 拖动：标题栏空白区 + 标题文字（注意 target=advisor_win，否则会拖动主面板）
        for w in (a_head, a_title):
            w.bind("<ButtonPress-1>", lambda e: self._drag_start(e, self.advisor_win))
            w.bind("<B1-Motion>", self._drag_move)
            w.bind("<Double-Button-1>", lambda e: self._advisor_maxtoggle(lbl_max))

        # 内容区 (Canvas + Scrollbar)
        a_wrap = tk.Frame(self.advisor_win, bg=C_BG)
        a_wrap.pack(fill="both", expand=True, padx=10, pady=5)

        self.a_canvas = tk.Canvas(a_wrap, bg=C_BG, highlightthickness=0)
        a_sb = ttk.Scrollbar(a_wrap, orient="vertical", command=self.a_canvas.yview)
        self.a_canvas.configure(yscrollcommand=a_sb.set)
        self.a_frame = tk.Frame(self.a_canvas, bg=C_BG)

        self._a_win = self.a_canvas.create_window((0, 0), window=self.a_frame, anchor="nw")
        self.a_canvas.pack(side="left", fill="both", expand=True)
        a_sb.pack(side="right", fill="y")

        # 内容宽度必须跟随画布：否则窗口拉大只会多出右侧空白，表格不会变宽
        def _on_canvas_cfg(e):
            try:
                self.a_canvas.itemconfigure(self._a_win, width=e.width)
            except Exception:
                pass
            self.a_canvas.configure(scrollregion=self.a_canvas.bbox("all"))

        self.a_canvas.bind("<Configure>", _on_canvas_cfg)
        self.a_frame.bind("<Configure>",
                          lambda e: self.a_canvas.configure(scrollregion=self.a_canvas.bbox("all")))
        # 滚轮绑在 Toplevel 上：Toplevel 在所有子控件的 bindtags 里，悬停任意子组件都能滚动
        self.advisor_win.bind("<MouseWheel>",
                              lambda e: self.a_canvas.yview_scroll(int(-1 * (e.delta / 120)), "units"))

        # 底部状态栏：缩放抓手（右下角拖动改尺寸）
        a_status = tk.Frame(self.advisor_win, bg=C_CARD, height=16)
        a_status.pack(fill="x", side="bottom")
        a_status.pack_propagate(False)
        self.adv_grip = tk.Label(a_status, text="◢", fg="#6b7380", bg=C_CARD,
                                 font=(FONT, 9), cursor="size_nw_se", width=2)
        self.adv_grip.pack(side="right", padx=(0, 2))
        self.adv_grip.bind("<ButtonPress-1>", self._adv_resize_start)
        self.adv_grip.bind("<B1-Motion>", self._adv_resize_move)
        self.adv_grip.bind("<Double-Button-1>", lambda e: self._adv_reset_size())

        # 右边缘 / 下边缘细拖动带（5px）：不用精准点中小抓手也能拉
        for kw, cur in (
                (dict(relx=1.0, rely=0.0, anchor="ne", relheight=1.0, width=5),
                 "sb_h_double_arrow"),
                (dict(relx=0.0, rely=1.0, anchor="sw", relwidth=1.0, height=5),
                 "sb_v_double_arrow")):
            band = tk.Frame(self.advisor_win, bg=C_BG, cursor=cur,
                            bd=0, highlightthickness=0)
            band.place(**kw)
            band.bind("<ButtonPress-1>", self._adv_resize_start)
            band.bind("<B1-Motion>", self._adv_resize_move)

        # 悬浮球（初始隐藏）
        self.ball = tk.Frame(self, bg=C_CARD, width=112, height=40)
        self.ball_lbl = tk.Label(self.ball, text="GPU 0%", bg=C_CARD, fg=C_GREEN,
                                 font=(MONO, 13, "bold"))
        self.ball_lbl.place(relx=0.5, rely=0.5, anchor="center")
        for w in (self.ball, self.ball_lbl):
            w.bind("<ButtonPress-1>", self._drag_start)
            w.bind("<B1-Motion>", self._drag_move)
            w.bind("<Double-Button-1>", lambda e: self.toggle_mini())
            w.bind("<Button-3>", lambda e: self._tray_menu())

    def _gpu_card(self, key: str) -> dict:
        if key in self.gpu_cards:
            return self.gpu_cards[key]
        c = tk.Frame(self.gpu_frame, bg=C_CARD, padx=9, pady=7)
        c.pack(fill="x", pady=(0, 6))
        top = tk.Frame(c, bg=C_CARD)
        top.pack(fill="x")
        lutil = tk.Label(top, text="0%", bg=C_CARD, fg=C_GREEN, font=(MONO, 11, "bold"))
        lutil.pack(side="right")
        lname = tk.Label(top, text="GPU", bg=C_CARD, fg=C_FG,
                         font=(FONT, 9, "bold"), anchor="w")
        lname.pack(side="left", fill="x", expand=True)

        b1 = Bar(c)
        b1.pack(fill="x", pady=(5, 4))
        mid = tk.Frame(c, bg=C_CARD)
        mid.pack(fill="x")
        lmem = tk.Label(mid, text="显存 —", bg=C_CARD, fg=C_SUB, font=(FONT, 8))
        lmem.pack(side="left")
        lmemv = tk.Label(mid, text="", bg=C_CARD, fg=C_SUB, font=(MONO, 8))
        lmemv.pack(side="right")
        b2 = Bar(c)
        b2.pack(fill="x", pady=(3, 5))

        info = tk.Frame(c, bg=C_CARD)
        info.pack(fill="x")
        ltemp = tk.Label(info, text="—", bg=C_CARD, fg=C_SUB, font=(MONO, 8))
        ltemp.pack(side="left")
        lfan = tk.Label(info, text="", bg=C_CARD, fg=C_SUB, font=(MONO, 8))
        lfan.pack(side="left", padx=(10, 0))
        lpow = tk.Label(info, text="", bg=C_CARD, fg=C_SUB, font=(MONO, 8))
        lpow.pack(side="right")

        d = dict(frame=c, name=lname, util=lutil, bar1=b1, mem=lmem, memv=lmemv,
                 bar2=b2, temp=ltemp, fan=lfan, pow=lpow)
        self.gpu_cards[key] = d
        return d

    # ------------------------------------------------------------ 交互

    def _drag_start(self, e, win=None):
        self._drag_win = win or self
        self._dx, self._dy = e.x, e.y

    def _drag_move(self, e):
        win = getattr(self, "_drag_win", None) or self
        try:
            win.geometry("+%d+%d" % (self.winfo_pointerx() - self._dx,
                                     self.winfo_pointery() - self._dy))
        except Exception:
            pass

    # ---- 主面板缩放（无边框窗口只能手动实现） ----

    def _on_main_configure(self, e):
        """任何来源的尺寸变化都同步到记忆值（拖动 / 程序设置 / DPI 变化）。"""
        if e.widget is not self or self.mini:
            return
        if e.width > 100 and e.height > 100:
            self.W, self.H = e.width, e.height

    def _resize_start(self, e):
        # 记录拖动起点的绝对指针坐标：抓手本身会随窗口移动，
        # 用 e.x / e.y（相对控件）会越拖越偏，甚至完全拖不动。
        self._rs = (self.winfo_width(), self.winfo_height(),
                    self.winfo_pointerx(), self.winfo_pointery())

    def _resize_move(self, e):
        if not getattr(self, "_rs", None):
            return
        w0, h0, px0, py0 = self._rs
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        w = max(getattr(self, "_min_w", MIN_W),
                min(sw, w0 + (self.winfo_pointerx() - px0)))
        h = max(MIN_H, min(sh, h0 + (self.winfo_pointery() - py0)))
        self.W, self.H = int(w), int(h)
        try:
            self.geometry("%dx%d" % (self.W, self.H))
        except Exception:
            pass

    def _reset_size(self):
        """双击抓手恢复默认尺寸。"""
        self.W = max(DEFAULT_W, getattr(self, "_min_w", DEFAULT_W))
        self.H = DEFAULT_H
        self.geometry("%dx%d" % (self.W, self.H))

    # ---- 进程表排序 ----

    def _update_headings(self):
        base = {"name": "进程", "pid": "PID", "mem": "显存", "util": "GPU"}
        for c, label in base.items():
            if c == self.sort_col:
                label += " ▼" if self.sort_desc else " ▲"
            self.tree.heading(c, text=label,
                              command=lambda cc=c: self._sort_by(cc))

    def _sort_by(self, col: str):
        if self.sort_col == col:
            self.sort_desc = not self.sort_desc
        else:
            self.sort_col = col
            self.sort_desc = (col != "name")   # 名字升序，数值降序
        self._update_headings()
        if self.snap:
            self._render(self.snap)

    def _sorted_procs(self, procs: List[GpuProc]) -> List[GpuProc]:
        key = {"name": lambda p: p.name.lower(), "pid": lambda p: p.pid,
               "mem": lambda p: p.mem_mb, "util": lambda p: p.util}.get(self.sort_col)
        if key is None:
            return procs
        return sorted(procs, key=key, reverse=self.sort_desc)

    # ---- 模型顾问缩放 ----

    def _adv_resize_start(self, e):
        self._rs = (self.advisor_win.winfo_width(), self.advisor_win.winfo_height(),
                    self.winfo_pointerx(), self.winfo_pointery())

    def _adv_resize_move(self, e):
        if not getattr(self, "_rs", None):
            return
        w0, h0, px0, py0 = self._rs
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        w = max(getattr(self, "_adv_min_w", ADV_MIN_W),
                min(sw, w0 + (self.winfo_pointerx() - px0)))
        h = max(ADV_MIN_H, min(sh, h0 + (self.winfo_pointery() - py0)))
        self.adv_w, self.adv_h = int(w), int(h)
        # 手动缩放后取消"最大化"还原点，避免还原时尺寸错乱
        self._adv_saved_geo = None
        try:
            self.advisor_win.geometry("%dx%d" % (self.adv_w, self.adv_h))
        except Exception:
            pass

    def _adv_reset_size(self):
        """双击抓手恢复模型顾问默认尺寸。"""
        self.adv_w, self.adv_h = ADV_DEFAULT_W, ADV_DEFAULT_H
        self._adv_saved_geo = None
        self.advisor_win.geometry("%dx%d" % (self.adv_w, self.adv_h))

    def toggle_mini(self):
        """切换 悬浮球 <-> 完整面板。"""
        if self.mini:
            x, y = self.winfo_x(), self.winfo_y()
            self.ball.pack_forget()
            self.bar.pack(fill="x")
            self.body.pack(fill="both", expand=True, padx=10, pady=(8, 0))
            self.geometry("%dx%d+%d+%d" % (self.W, self.H, x, y))
            self.mini = False
        else:
            x, y = self.winfo_x(), self.winfo_y()
            self.bar.pack_forget()
            self.body.pack_forget()
            self.geometry("112x40+%d+%d" % (x, y))
            self.ball.pack(fill="both", expand=True)
            self.mini = True

    def toggle_hide(self):
        """隐藏到系统托盘 / 恢复。"""
        if self.hidden:
            self.deiconify()
            self.lift()
            self.hidden = False
            self.collector.interval = self.cfg.get("interval", 1500) / 1000.0
        else:
            self.withdraw()
            self.hidden = True
            self.collector.interval = 5.0   # 隐藏时降频省电
            self.tray.show(self.last_icon_util if self.last_icon_util >= 0 else 0)

    def _start_hotkey(self):
        self.hk = HotkeyThread(lambda: self.after(0, self._hotkey_toggle))
        self.hk.start()

    def _hotkey_toggle(self):
        if self.hidden:
            self.toggle_hide()
        else:
            self.lift()
            self.focus_force()

    # ------------------------------------------------------------ 托盘

    def _setup_tray(self):
        self.tray = TrayIcon(self.winfo_id())
        # explorer 重启后托盘图标会被系统清空，监听 TaskbarCreated 广播自动重建
        _user32.RegisterWindowMessageW.restype = wintypes.UINT
        self._msg_taskbar = _user32.RegisterWindowMessageW("TaskbarCreated")
        self._proc_cb = WNDPROCTYPE(self._wndproc)
        self._old_proc = _user32.SetWindowLongPtrW(
            wintypes.HWND(self.winfo_id()), GWL_WNDPROC,
            ctypes.cast(self._proc_cb, ctypes.c_void_p))
        self.tray.show(0)

    def _wndproc(self, hwnd, msg, wparam, lparam):
        if msg == WM_TRAY:
            if lparam == WM_LBUTTONUP:
                self.after(0, self._tray_left_click)
                return 0
            if lparam == WM_RBUTTONUP:
                self.after(0, self._tray_menu)
                return 0
        if msg == self._msg_taskbar:
            self.tray.added = False
            self.tray.show(self.last_icon_util if self.last_icon_util >= 0 else 0.0)
            return 0
        return _user32.CallWindowProcW(ctypes.c_void_p(self._old_proc),
                                       wintypes.HWND(hwnd), msg, wparam, lparam)

    def _tray_left_click(self):
        self.toggle_hide()

    def _tray_menu(self):
        items = [(1, "显示 / 隐藏面板  (Ctrl+Alt+G)"),
                 (2, "悬浮球模式"),
                 (None, None),
                 (3, "刷新率: 1 秒"), (4, "刷新率: 2 秒"), (5, "刷新率: 3 秒"),
                 (None, None),
                 (6, "置顶: 开" if self.attributes("-topmost") else "置顶: 关"),
                 (None, None),
                 (9, "退出")]
        cmd = popup_menu(self.winfo_id(), items)
        if cmd == 1:
            self.toggle_hide()
        elif cmd == 2:
            if self.hidden:
                self.toggle_hide()
            self.toggle_mini()  # 再点一次可从悬浮球恢复完整面板
        elif cmd in (3, 4, 5):
            self.collector.interval = {3: 1.0, 4: 2.0, 5: 3.0}[cmd]
            self.cfg["interval"] = int(self.collector.interval * 1000)
        elif cmd == 6:
            cur = bool(self.attributes("-topmost"))
            self.attributes("-topmost", not cur)
            self.cfg["topmost"] = not cur
        elif cmd == 9:
            self.quit_app()

    def _row_menu(self, e):
        iid = self.tree.identify_row(e.y)
        if iid:
            self.tree.selection_set(iid)
        items = [(1, "结束该进程"), (2, "结束该进程及子进程"),
                 (None, None), (3, "在资源管理器中定位")]
        cmd = popup_menu(self.winfo_id(), items)
        if cmd == 1:
            self.kill_selected(tree=False)
        elif cmd == 2:
            self.kill_selected(tree=True)
        elif cmd == 3:
            self.locate_selected()

    # ------------------------------------------------------------ 模型顾问

    def show_advisor(self):
        """显示/隐藏模型顾问窗口。首次或切换设备时才重建内容，其余秒开。"""
        if self.advisor_win.winfo_viewable():
            self.advisor_win.withdraw()
            return
        # 内容只在首次 / 设备切换时渲染（探测线程启动后 local 很快命中缓存）
        if self._advisor_key != self.advisor_device:
            self._render_advisor()
            self._advisor_key = self.advisor_device
        # 定位在主面板右侧（钳制在屏幕内）
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        # 以记录值为准：未映射窗口的 winfo_width 返回的是"内容自然宽度"，
        # 直接拿来用会把首次打开的尺寸带偏
        w, h = self.adv_w, self.adv_h
        w = max(getattr(self, "_adv_min_w", ADV_MIN_W), w)
        w = max(ADV_MIN_W, min(sw - 20, w))
        h = max(ADV_MIN_H, min(sh - 40, h))
        ax = self.winfo_x() + self.winfo_width() + 10
        if ax + w > sw - 10:                   # 右侧放不下就贴屏幕右缘
            ax = max(10, sw - w - 12)
        ay = max(10, min(self.winfo_y(), sh - h - 60))
        self.advisor_win.geometry(f"{w}x{h}+{ax}+{ay}")
        self.advisor_win.deiconify()
        self.advisor_win.focus_force()

    def _advisor_maxtoggle(self, lbl):
        """顾问窗口最大化 / 还原。"""
        if self._adv_saved_geo:
            self.advisor_win.geometry(self._adv_saved_geo)
            try:
                size = self._adv_saved_geo.split("+")[0]
                w, h = size.split("x")
                self.adv_w, self.adv_h = int(w), int(h)
            except Exception:
                pass
            self._adv_saved_geo = None
            lbl.config(text="□")
        else:
            sw = self.advisor_win.winfo_screenwidth()
            sh = self.advisor_win.winfo_screenheight()
            self._adv_saved_geo = "%dx%d+%d+%d" % (
                self.advisor_win.winfo_width(), self.advisor_win.winfo_height(),
                self.advisor_win.winfo_x(), self.advisor_win.winfo_y())
            self.advisor_win.geometry("%dx%d+%d+%d" % (min(900, sw - 80), min(920, sh - 80), 40, 40))
            lbl.config(text="❐")

    def _render_advisor(self):
        """渲染模型顾问内容。"""
        # 导入引擎
        import sys
        _mpath = os.path.join(BASE_DIR, "model_advisor.py")
        if _mpath not in sys.path:
            sys.path.insert(0, BASE_DIR)

        try:
            import model_advisor as MA
        except ImportError:
            # 清空旧内容并显示错误
            for w in self.a_frame.winfo_children():
                w.destroy()
            tk.Label(self.a_frame, text="❌ 模型顾问引擎未找到", fg=C_RED,
                     bg=C_BG, font=(FONT, 10)).pack(pady=40)
            return

        # 清空旧内容
        for w in self.a_frame.winfo_children():
            w.destroy()

        # 设备选择 (本机 / 远程部署目标机)
        dev_fr = tk.Frame(self.a_frame, bg=C_BG)
        dev_fr.pack(fill="x", padx=10, pady=(8, 0))
        tk.Label(dev_fr, text="🎯 推荐目标:", fg="#a0a0a0", bg=C_BG,
                 font=(FONT, 8)).pack(side="left")
        # 必须持有 StringVar 引用，否则会被 GC，下拉框显示为空
        self._adv_dev_var = tk.StringVar(
            value=MA.DEVICE_LABELS.get(self.advisor_device, "本机"))
        dev_cb = ttk.Combobox(dev_fr, textvariable=self._adv_dev_var,
                              values=list(MA.DEVICE_LABELS.values()),
                              state="readonly", width=24, font=(FONT, 8))
        dev_cb.pack(side="left", padx=6)

        def _on_device(event=None):
            label = dev_cb.get()
            self.advisor_device = next(
                (k for k, v in MA.DEVICE_LABELS.items() if v == label), "local")
            self._advisor_key = self.advisor_device
            self._render_advisor()

        dev_cb.bind("<<ComboboxSelected>>", _on_device)

        # 加载所选设备配置
        config = MA.get_device_config(self.advisor_device)
        vram_mb = config["gpu"]["vram_mb"]
        ram_mb = config.get("ram_mb", 32768)
        cpu_cores = config.get("cpu", {}).get("cores", 16)
        gpu_name = config["gpu"].get("name", "Unknown")

        # 标题: 所选设备配置摘要
        hdr = tk.Frame(self.a_frame, bg=C_CARD, padx=10, pady=8)
        hdr.pack(fill="x", padx=10, pady=(6, 4))
        lbl_gpu = tk.Label(hdr, text=f"🖥️ {gpu_name}", fg=C_FG, bg=C_CARD,
                           font=(FONT, 9, "bold"), anchor="w", justify="left")
        lbl_gpu.pack(anchor="w", fill="x")
        tk.Label(hdr, text=f"{round(vram_mb / 1024)}GB 显存 · "
                           f"{ram_mb//1024:.0f}GB 内存 · {cpu_cores} 核 CPU",
                 fg="#a0a0a0", bg=C_CARD, font=(FONT, 8)).pack(anchor="w")
        # 长显卡名按窗口宽度换行，避免右侧被裁掉
        hdr.bind("<Configure>",
                 lambda e: lbl_gpu.config(wraplength=max(240, e.width - 24)))
        if config.get("note"):
            tk.Label(hdr, text=config["note"], fg="#a0a0a0", bg=C_CARD,
                     font=(FONT, 7)).pack(anchor="w")

        # 引擎计算
        advisor = MA.ModelAdvisor(vram_mb, ram_mb, cpu_cores)
        summary = advisor.recommend()

        # 按适应度排序 (从上到下越适合当前设备):
        #   Q4_K_M 甜点 > 更高质量量化 > 低量化; 同分时上下文长、显存吃得满的优先
        #   (最能打的排最前); 装不下的 (ctx=0) 垫底
        QUANT_FIT = {"Q4_K_M": 6, "Q5_K_M": 5, "Q6_K": 4, "Q8_0": 3,
                     "FP16": 2, "Q3_K_L": 1, "Q3_K_S": 1, "Q2_K": 0}

        def _fit_key(item):
            name, rec = item
            ctx = rec["max_context_tokens"]
            if ctx <= 0:
                return (-1.0, 0.0)
            q = QUANT_FIT.get(rec["best_quant"], 0)
            ctx_bonus = min(ctx, 262144) / 262144
            vram_use = min(rec["recommended_vram_mb"] / max(vram_mb, 1), 1.0)
            return (q * 100 + ctx_bonus * 20 + vram_use * 10, 0.0)

        items = sorted(summary.items(), key=_fit_key, reverse=True)
        n_total = len(items)
        n_ok = sum(1 for _, r in items if r["max_context_tokens"] > 0)

        # 工具条: 过滤 + 复制
        bar = tk.Frame(self.a_frame, bg=C_BG)
        bar.pack(fill="x", padx=10, pady=(4, 2))
        tk.Label(bar, text=f"📋 推荐结果   可运行 {n_ok}/{n_total}", fg=C_FG,
                 bg=C_BG, font=(FONT, 9, "bold")).pack(side="left")

        # 必须持有 BooleanVar 引用：局部变量被 GC 后勾选框会失效
        self._adv_only_var = tk.BooleanVar(value=self._adv_feasible_only)
        only_ok = self._adv_only_var

        def _toggle_only():
            self._adv_feasible_only = bool(only_ok.get())
            self._render_advisor()

        tk.Checkbutton(bar, text="只看能跑", variable=only_ok, command=_toggle_only,
                       bg=C_BG, fg=C_SUB, selectcolor=C_CARD2,
                       activebackground=C_BG, activeforeground=C_FG,
                       font=(FONT, 8), bd=0, highlightthickness=0
                       ).pack(side="right")
        tk.Button(bar, text="复制", command=self._copy_advisor, bg=C_CARD,
                  fg=C_SUB, activebackground=C_CARD2, activeforeground=C_FG,
                  relief="flat", bd=0, font=(FONT, 7), cursor="hand2",
                  padx=7, pady=1).pack(side="right", padx=(0, 8))

        if self._adv_feasible_only:
            items = [(n, r) for n, r in items if r["max_context_tokens"] > 0]

        # 表格用 grid + 列权重: 窗口拉宽时各列按比例铺开，而不是只多出右侧空白
        table = tk.Frame(self.a_frame, bg=C_BG)
        table.pack(fill="x", padx=10, pady=(4, 2))
        for c, wt in enumerate((5, 2, 2, 2, 2)):
            table.grid_columnconfigure(c, weight=wt, uniform="adv")
        for c, h in enumerate(("模型", "量化", "上下文", "显存", "状态")):
            tk.Label(table, text=h, fg=C_SUB, bg=C_BG, font=(FONT, 8, "bold"),
                     anchor="w").grid(row=0, column=c, sticky="ew", padx=(2, 6))

        self._adv_rows = []
        for r, (name, rec) in enumerate(items, start=1):
            bg_color = C_CARD if rec["best_quant"] == "Q4_K_M" else C_BG
            moe_tag = " [MoE]" if rec["is_moe"] else ""

            # 上下文长度 (转 K tokens)
            ctx_k = rec["max_context_tokens"] / 1024 if rec["max_context_tokens"] > 0 else 0
            ctx_txt = f"{ctx_k:.0f}K" if ctx_k >= 1 else f"{rec['max_context_tokens']} tok"

            # 显存 (GB)
            vram_gb = rec["recommended_vram_mb"] / 1024
            if vram_gb < 16:
                vram_color = "#66bb6a"
            elif vram_gb < 22:
                vram_color = "#ffa726"
            else:
                vram_color = "#ef5350"

            # 状态指示器
            if rec["max_context_tokens"] <= 0:
                status = "❌ 装不下"
            elif rec["best_quant"] in ("Q4_K_M", "Q5_K_M"):
                status = "✅ 推荐"
            elif rec["max_context_tokens"] < 4096:
                status = "⚠️ 极限"
            else:
                status = "ℹ️ 可用"

            q_color = "#6fb1d9" if rec["best_quant"] in ("Q4_K_M", "Q5_K_M") else "#a0a0a0"
            cells = (
                (f"{name}{moe_tag}", C_FG, (FONT, 8)),
                (rec["best_quant"], q_color, (FONT, 8, "bold")),
                (ctx_txt, C_FG, (FONT, 8)),
                (f"{vram_gb:.1f}GB", vram_color, (FONT, 8, "bold")),
                (status, "#a0a0a0", (FONT, 7)),
            )
            for c, (text, fg, font) in enumerate(cells):
                tk.Label(table, text=text, fg=fg, bg=bg_color, font=font,
                         anchor="w").grid(row=r, column=c, sticky="ew",
                                          padx=(2, 6), pady=1)
            self._adv_rows.append((f"{name}{moe_tag}", rec["best_quant"],
                                   ctx_txt, f"{vram_gb:.1f}GB", status))

        # 底部说明
        note_fr = tk.Frame(self.a_frame, bg=C_BG, padx=10, pady=6)
        note_fr.pack(fill="x")
        tk.Label(note_fr, text="💡 Q4_K_M 为质量/速度平衡点 | MoE=混合专家模型 (激活参数少，推理快)\n"
                       "显存 = 权重 + KV Cache (默认上下文 128K tokens, KV 按 Q4 量化估算) | VRAM 充裕时推荐高质量量化",
                 fg="#5c6370", bg=C_BG, font=(FONT, 7), justify="left",
                 wraplength=390).pack(anchor="w")

        # 按内容实际需要算最小宽度：高 DPI 下字号更大，固定 460px 会把列截断
        try:
            self.update_idletasks()
            need = self.a_frame.winfo_reqwidth() + 44
            self._adv_min_w = max(
                ADV_MIN_W, min(need, self.winfo_screenwidth() - 40))
        except Exception:
            pass

    def _copy_advisor(self):
        """把当前推荐表复制到剪贴板（制表符分隔，可直接粘进 Excel）。"""
        rows = getattr(self, "_adv_rows", [])
        if not rows:
            return
        txt = "\n".join(["模型\t量化\t上下文\t显存\t状态"] +
                        ["\t".join(r) for r in rows])
        try:
            self.clipboard_clear()
            self.clipboard_append(txt)
            self.update_idletasks()
            self.lbl_status.config(text="已复制 %d 条推荐到剪贴板" % len(rows))
        except Exception:
            pass

    # ------------------------------------------------------------ 设置

    def show_settings(self):
        win = tk.Toplevel(self)
        win.title("设置")
        win.configure(bg=C_BG)
        win.attributes("-topmost", True)
        win.geometry("288x342+%d+%d" % (max(10, self.winfo_x() - 298),
                                        max(10, self.winfo_y())))
        tk.Label(win, text="刷新间隔", bg=C_BG, fg=C_SUB, font=(FONT, 8)).pack(
            anchor="w", padx=14, pady=(14, 2))
        v = tk.IntVar(value=int(self.collector.interval * 1000))
        for ms, txt in ((500, "0.5 秒"), (1000, "1 秒"), (1500, "1.5 秒"),
                        (2000, "2 秒"), (3000, "3 秒")):
            tk.Radiobutton(win, text=txt, variable=v, value=ms, bg=C_BG, fg=C_FG,
                           selectcolor=C_CARD2, activebackground=C_BG,
                           activeforeground=C_FG, font=(FONT, 9),
                           command=lambda m=ms: self._set_interval(m / 1000.0)
                           ).pack(anchor="w", padx=24)
        tk.Label(win, text="不透明度", bg=C_BG, fg=C_SUB, font=(FONT, 8)).pack(
            anchor="w", padx=14, pady=(10, 2))
        a = tk.Scale(win, from_=0.5, to=1.0, resolution=0.02, orient="horizontal",
                     bg=C_BG, fg=C_FG, troughcolor=C_CARD2, highlightthickness=0,
                     activebackground=C_BG, bd=0,
                     command=lambda x: self._set_alpha(float(x)))
        a.set(self.alpha)
        a.pack(fill="x", padx=14)
        tk.Button(win, text="恢复默认面板尺寸", command=self._reset_size,
                  bg=C_CARD, fg=C_SUB, activebackground=C_CARD2,
                  activeforeground=C_FG, relief="flat", bd=0,
                  font=(FONT, 8), cursor="hand2").pack(fill="x", padx=14,
                                                       pady=(10, 0))
        tk.Label(win, text="右下角 ◢ 可拖动缩放，双击恢复默认\n"
                           "模型顾问同样支持右下角 / 右边缘 / 下边缘拖动\n"
                           "悬浮球 / 隐藏 / 退出：标题栏 ◎ — ✕\n"
                           "全局热键 Ctrl+Alt+G，托盘图标右键菜单\n"
                           "v" + APP_VERSION,
                 bg=C_BG, fg="#5c6370", font=(FONT, 7), justify="left").pack(
            anchor="w", padx=14, pady=(8, 10))

    def _set_interval(self, sec: float):
        self.collector.interval = sec
        self.cfg["interval"] = int(sec * 1000)

    def _set_alpha(self, v: float):
        self.alpha = v
        self.attributes("-alpha", v)

    # ------------------------------------------------------------ 结束进程

    def selected_proc(self) -> Optional[GpuProc]:
        sel = self.tree.selection()
        if not sel:
            return None
        return self.proc_rows.get(sel[0])

    def llm_mode(self):
        """大模型模式: 结束所有非白名单的占显存进程, 为推理让出显存。"""
        snap = self.snap
        if not snap or not snap.procs:
            messagebox.showinfo(APP_NAME, "暂无 GPU 进程数据，稍后再试。", parent=self)
            return
        my_pids = {os.getpid(), os.getppid()}
        targets = [p for p in snap.procs
                   if p.name.lower() not in LLM_MODE_WHITELIST
                   and p.pid not in PROTECTED_PIDS and p.pid not in my_pids]
        if not targets:
            messagebox.showinfo("🚀 大模型模式", "显存干净，无需清理。", parent=self)
            return
        desc = "\n".join("  %s (PID %d, %s)" % (
            p.name, p.pid, hr_bytes(p.mem_mb) if p.mem_mb else "未知")
            for p in targets)
        if not messagebox.askyesno(
                "🚀 大模型模式",
                "将结束以下 %d 个占显存进程（系统/推理程序已保护）：\n\n%s\n\n确定继续？"
                % (len(targets), desc), icon="warning", parent=self):
            return
        killed, errs = [], []
        for p in targets:
            e = terminate_process(p.pid)
            if e:
                errs.append("%s (PID %d): %s" % (p.name, p.pid, e))
            else:
                killed.append("%s (PID %d)" % (p.name, p.pid))
        msg = "已结束 %d 个进程，显存让给大模型。" % len(killed)
        if errs:
            msg += "\n\n失败 %d 个（受保护进程，属正常）：\n" % len(errs)
            msg += "\n".join("  " + e for e in errs[:6])
        messagebox.showinfo("🚀 大模型模式", msg, parent=self)

    def kill_selected(self, tree: bool = False):
        p = self.selected_proc()
        if not p:
            messagebox.showinfo(APP_NAME, "请先在列表中选中一个进程。", parent=self)
            return
        name_l = p.name.lower()
        if p.pid in PROTECTED_PIDS or name_l in PROTECTED_NAMES:
            messagebox.showerror(
                APP_NAME,
                "已阻止：%s (PID %d) 属于系统关键进程，结束它会导致系统不稳定。\n\n"
                "如果确定要操作，请使用任务管理器或命令行自行处理。"
                % (p.name, p.pid), parent=self)
            return
        if p.pid == os.getpid() or p.pid == os.getppid():
            messagebox.showerror(APP_NAME, "不允许结束本面板自身进程。", parent=self)
            return

        desc = ("进程：%s\nPID：%d\n显存占用：%s\nGPU 占用：%.0f%%"
                % (p.name, p.pid, hr_bytes(p.mem_mb) if p.mem_mb else "未知", p.util))
        if not messagebox.askyesno(
                "确认结束进程",
                desc + "\n\n强制结束进程会导致其中未保存的数据丢失。\n确定继续？",
                icon="warning", parent=self):
            return

        pids = [p.pid]
        if tree:
            pids += child_pids(snapshot_processes(), p.pid)

        errs = []
        for pid in pids:
            e = terminate_process(pid)
            if e:
                errs.append("PID %d: %s" % (pid, e))
        if errs:
            messagebox.showerror(APP_NAME, "部分进程未能结束：\n\n" + "\n".join(errs),
                                 parent=self)
        else:
            self.after(400, self.force_refresh)

    def locate_selected(self):
        p = self.selected_proc()
        if not p:
            return
        try:
            procs = snapshot_processes()
            path = self._exe_path(p.pid)
            if path and os.path.exists(path):
                subprocess.Popen(["explorer", "/select,", path],
                                 creationflags=CREATE_NO_WINDOW)
            else:
                messagebox.showinfo(APP_NAME, "无法定位：%s\n%s"
                                    % (p.name, path or "路径不可用"), parent=self)
        except Exception as e:
            messagebox.showerror(APP_NAME, str(e), parent=self)

    @staticmethod
    def _exe_path(pid: int) -> Optional[str]:
        try:
            h = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not h:
                return None
            buf = (wintypes.WCHAR * 4096)()
            size = wintypes.DWORD(4096)
            ok = _kernel32.QueryFullProcessImageNameW(h, 0, buf, byref(size))
            _kernel32.CloseHandle(h)
            return buf.value if ok else None
        except Exception:
            return None

    # ------------------------------------------------------------ 刷新

    def force_refresh(self):
        """清空队列并立刻重采一次。"""
        while not self.q.empty():
            try:
                self.q.get_nowait()
            except Exception:
                break
        threading.Thread(target=lambda: self.q.put(self.collector.collect()),
                         daemon=True).start()

    def _poll(self):
        try:
            snap = None
            while not self.q.empty():
                try:
                    snap = self.q.get_nowait()
                except Exception:
                    break
            if snap:
                self.snap = snap
                self._render(snap)
        except Exception as e:
            # 渲染出错绝不能让 after 链断掉（否则面板永久冻结）；同一错误只记一次日志
            key = repr(e)
            if key != getattr(self, "_last_poll_err", None):
                self._last_poll_err = key
                _log_crash("poll/render", e)
        finally:
            self.after(400, self._poll)

    def _render(self, s: Snapshot):
        self.lbl_src.config(text="· " + s.source)

        # 进程表
        sel = self.tree.selection()
        sel_pid = (self.proc_rows[sel[0]].pid
                   if sel and sel[0] in self.proc_rows else None)
        self.tree.delete(*self.tree.get_children())
        self.proc_rows.clear()
        new_sel = None
        for p in self._sorted_procs(s.procs)[:200]:
            name = p.name
            short = name if len(name) <= 22 else name[:20] + "…"
            tag = "sys" if name.lower() in PROTECTED_NAMES else (
                "hot" if p.util >= 60 else "")
            iid = self.tree.insert(
                "", "end",
                values=(short, p.pid,
                        hr_bytes(p.mem_mb) if p.mem_mb >= 1 else "—",
                        "%.0f%%" % p.util if p.util >= 1 else "—"),
                tags=(tag,))
            self.proc_rows[iid] = p
            if sel_pid is not None and p.pid == sel_pid:
                new_sel = iid
        if new_sel:
            self.tree.selection_set(new_sel)
        self.lbl_pcount.config(text="GPU 进程 (%d)" % len(s.procs))

        # GPU 卡片
        keys = {g.key for g in s.gpus}
        for k in list(self.gpu_cards):
            if k not in keys:
                self.gpu_cards[k]["frame"].destroy()
                del self.gpu_cards[k]

        total_util = 0.0
        for g in s.gpus[:4]:
            d = self._gpu_card(g.key)
            d["name"].config(text=short_gpu_name(g.name))
            d["util"].config(text="%.0f%%" % g.util, fg=load_color(g.util))
            d["bar1"].set(g.util)
            if g.mem_total and g.mem_total > 0:
                pct = g.mem_used / g.mem_total * 100.0
                d["mem"].config(text="显存  %s / %s" % (hr_bytes(g.mem_used),
                                                        hr_bytes(g.mem_total)))
                d["memv"].config(text="%.0f%%" % pct)
                d["bar2"].set(pct, C_ACCENT)
            elif g.mem_used > 0:
                d["mem"].config(text="显存占用 %s" % hr_bytes(g.mem_used))
                d["memv"].config(text="")
                d["bar2"].set(0)
            else:
                d["mem"].config(text="显存 —")
                d["memv"].config(text="")
                d["bar2"].set(0)
            d["temp"].config(text=("%.0f°C" % g.temp) if g.temp is not None else "—")
            d["fan"].config(text=("风扇 %.0f%%" % g.fan) if g.fan is not None else "")
            d["pow"].config(text=("%.0f W" % g.power) if g.power is not None else "")
            total_util = max(total_util, g.util)

        if not s.gpus:
            self.lbl_pcount.config(text="GPU 进程 (%d) · 未检测到 GPU 数据" % len(s.procs))

        # 底部状态条：显存占用 + 进程数 + 更新时间
        g0 = s.gpus[0] if s.gpus else None
        if g0 and g0.mem_total:
            self.lbl_status.config(text="%s / %s · %d 进程 · %s" % (
                hr_bytes(g0.mem_used), hr_bytes(g0.mem_total), len(s.procs),
                time.strftime("%H:%M:%S")))
        else:
            self.lbl_status.config(text="%d 个 GPU 进程 · 更新 %s" % (
                len(s.procs), time.strftime("%H:%M:%S")))

        self.spark.push(total_util)

        # 悬浮球 / 托盘
        self.ball_lbl.config(text="GPU %.0f%%" % total_util, fg=load_color(total_util))
        tip = "%s  %.0f%%" % (APP_NAME, total_util)
        if s.gpus and s.gpus[0].mem_total:
            tip += "  显存 %s / %s" % (hr_bytes(s.gpus[0].mem_used),
                                       hr_bytes(s.gpus[0].mem_total))
        if abs(total_util - self.last_icon_util) >= 4 or self.last_icon_util < 0:
            self.last_icon_util = total_util
            self.tray.update(total_util, tip[:127])

    # ------------------------------------------------------------ 退出

    def quit_app(self):
        try:
            self.save_cfg()
            self.collector.stop()
            self.collector.join(timeout=2.0)  # 等采集线程退出再关 PDH，避免并发关闭崩溃
            self.collector.pdh.close()
            if self.hk:
                self.hk.stop()
            self.tray.remove()
        except Exception:
            pass
        self.destroy()


def _log_crash(stage: str, exc: BaseException):
    """崩溃/异常写入 crash.log, 供自启动场景排查 (pythonw 无控制台)。"""
    try:
        with open(os.path.join(BASE_DIR, "crash.log"), "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), stage))
            if exc is not None:
                f.write(traceback.format_exc() + "\n")
    except Exception:
        pass


def main():
    # 单实例保护：已有实例在跑就唤起它
    try:
        _kernel32.CreateMutexW(None, False, "GpuMonitorPanelMutex")
        if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
            _user32.MessageBoxW(None, "GPU 监视器 已经在运行中。\n"
                                      "（若面板未显示，请按 Ctrl+Alt+G 呼出，"
                                      "或查看系统托盘）",
                                "GPU 监视器", 0x40 | 0x1000)
            return
    except Exception:
        pass

    try:
        app = GPUPanel()
        _log_crash("started ok", None)
    except BaseException as e:
        _log_crash("startup crash", e)
        raise
    try:
        app.mainloop()
    except KeyboardInterrupt:
        pass
    except BaseException as e:
        _log_crash("mainloop crash", e)
        raise


if __name__ == "__main__":
    main()
