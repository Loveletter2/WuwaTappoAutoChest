#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WuwaTappo 自动开箱助手

原理：屏幕取色 + 轮廓识别，找到桌宠右侧的“宝箱气泡”，
校验该坐标处的窗口确实属于 WuwaTappo 后，模拟一次鼠标左键点击。

多显示器要点：
- 启动时把进程切到 Per-Monitor V2 DPI 感知，坐标统一使用物理像素；
- 按“显示器”分块截图、分块识别，尺寸阈值按该显示器的缩放比例换算，
  这样 100% / 125% / 150% 混用的多屏环境判定标准一致；
- 只在桌宠覆盖窗口所在的那块屏幕上找气泡，别的屏幕上的相似色块不参与；
- 两个宝箱气泡上下贴在一起时会被颜色掩膜连成一块，这里会沿“细腰”拆开，
  避免只认到一个宝箱、或者点到两个气泡中间的空隙。

不做内存读写、不修改存档、不调用私有接口，只做屏幕识别与点击。
"""

from __future__ import annotations

import argparse
import ctypes
from collections import deque
import ctypes.wintypes as wt
import json
import os
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageGrab

APP_NAME = "WuwaTappoAutoChest"
VERSION = "0.3.0"

BASE_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / APP_NAME
BASE_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_PATH = BASE_DIR / "config.json"
LOG_PATH = BASE_DIR / "autochest.log"

# 尺寸类阈值都是“96 DPI（100% 缩放）”下的像素值，
# 实际识别时按所在显示器的缩放比例换算，换屏幕/换缩放不会失效。
DEFAULT_CONFIG = {
    "interval_sec": 15,
    "auto_start": True,
    "click_wait_sec": 2.2,
    "verify_timeout_sec": 3.0,
    "max_clicks_per_round": 2,
    "retry_per_chest": 1,
    "fail_cooldown_sec": 120,
    "hsv_cream_low": [12, 70, 190],
    "hsv_cream_high": [28, 160, 255],
    "hsv_brown_low": [5, 80, 80],
    "hsv_brown_high": [22, 200, 200],
    "min_area": 300,
    "max_area": 9000,
    "min_wh": [16, 12],
    "max_wh": [140, 110],
    "brown_px_min": 90,
    "brown_block_min": 60,
    "brown_ratio_min": 0.12,
    "brown_solid_ratio_min": 0.35,
    # 宝箱图案本身的长宽比/占比（与缩放无关），用来区分“气泡里的宝箱”与壁纸上的棕色块
    "icon_aspect_min": 1.25,
    "icon_aspect_max": 3.0,
    "icon_w_ratio_min": 0.30,
    "icon_h_ratio_max": 0.65,
    "icon_offset_max": 0.25,
    "aspect_min": 1.05,
    "aspect_max": 2.2,
    "fill_min": 0.50,
    "fill_max": 0.95,
    # 桌宠贴着屏幕边缘时气泡会被屏幕边界裁掉一块，贴边的候选单独用更宽松的门槛
    "clipped_min_ratio": 0.45,
    "clipped_aspect_min": 0.45,
    "clipped_fill_min": 0.35,
    "icon_cream_ratio_min": 0.30,
}

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
shell32 = ctypes.windll.shell32

SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79

MONITOR_DEFAULTTONEAREST = 2
MDT_EFFECTIVE_DPI = 0

DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE = -3

MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

PET_PROCESS = "wuwatappo.exe"
# 桌宠的桌面覆盖窗口：识别/点击只允许落在这里面
PET_OVERLAY_CLASSES = ("DesktopCatInput",)
# 点击校验时允许出现的桌宠窗口类（除了覆盖窗口，设置面板、Toast 等也是这个进程）
PET_WINDOW_CLASSES = ("DesktopCatInput", "Tauri Window")

# 64 位下必须显式声明指针/句柄返回值，避免 ctypes 默认按 int 截断。
user32.WindowFromPoint.restype = wt.HWND
user32.WindowFromPoint.argtypes = [wt.POINT]
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
user32.GetCursorPos.argtypes = [ctypes.POINTER(wt.POINT)]
user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
user32.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.GetAncestor.restype = wt.HWND
user32.GetAncestor.argtypes = [wt.HWND, ctypes.c_uint]
kernel32.OpenProcess.restype = wt.HANDLE
kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
kernel32.CreateMutexW.restype = wt.HANDLE
kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.LPCWSTR]
kernel32.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
kernel32.CloseHandle.argtypes = [wt.HANDLE]
kernel32.GetCurrentProcess.restype = wt.HANDLE


# ---------------------------------------------------------------- DPI / 显示器


class MONITORINFOEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.DWORD),
        ("rcMonitor", wt.RECT),
        ("rcWork", wt.RECT),
        ("dwFlags", wt.DWORD),
        ("szDevice", wt.WCHAR * 32),
    ]


@dataclass(frozen=True)
class Monitor:
    """一块显示器的物理几何与 DPI。"""

    handle: int
    left: int
    top: int
    right: int
    bottom: int
    dpi: int
    primary: bool
    work: tuple[int, int, int, int] = (0, 0, 0, 0)
    name: str = ""

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top

    @property
    def scale(self) -> float:
        return max(0.5, self.dpi / 96.0)

    def __str__(self) -> str:
        tag = "主" if self.primary else "副"
        return f"[{tag} {self.name or '?'} {self.width}x{self.height}@{self.left},{self.top} {self.scale:.0%}]"


def get_dpi_for_monitor(hmon: int) -> int:
    """显示器实际 DPI；取不到时退回 96。"""
    try:
        shcore = ctypes.windll.shcore
        shcore.GetDpiForMonitor.argtypes = [
            wt.HMONITOR, ctypes.c_int,
            ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(ctypes.c_uint),
        ]
        shcore.GetDpiForMonitor.restype = ctypes.c_long
        dx = ctypes.c_uint()
        dy = ctypes.c_uint()
        if shcore.GetDpiForMonitor(wt.HMONITOR(hmon), MDT_EFFECTIVE_DPI, ctypes.byref(dx), ctypes.byref(dy)) == 0:
            return int(dx.value) or 96
    except Exception:
        pass
    return 96


def list_monitors() -> list[Monitor]:
    """按物理像素枚举所有显示器（左→右、上→下）。"""
    found: list[Monitor] = []
    proc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HMONITOR, wt.HDC, ctypes.POINTER(wt.RECT), wt.LPARAM)

    def _cb(hmon, _hdc, _rect, _data):
        info = MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(MONITORINFOEXW)
        if not user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
            return True
        r = info.rcMonitor
        w = info.rcWork
        found.append(Monitor(
            handle=int(hmon) if hmon else 0,
            left=r.left, top=r.top, right=r.right, bottom=r.bottom,
            dpi=get_dpi_for_monitor(hmon),
            primary=bool(info.dwFlags & 1),
            work=(w.left, w.top, w.right, w.bottom),
            name=info.szDevice or "",
        ))
        return True

    try:
        user32.EnumDisplayMonitors(None, None, proc(_cb), 0)
    except Exception:
        pass
    if not found:
        w = user32.GetSystemMetrics(0) or 1920
        h = user32.GetSystemMetrics(1) or 1080
        found.append(Monitor(0, 0, 0, w, h, get_dpi_for_monitor(0), True, (0, 0, w, h)))
    found.sort(key=lambda m: (m.left, m.top))
    return found


def dpi_awareness() -> str:
    """当前进程实际生效的 DPI 感知模式：unaware / system / permonitor。"""
    try:
        user32.GetThreadDpiAwarenessContext.restype = ctypes.c_void_p
        user32.GetAwarenessFromDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        user32.GetAwarenessFromDpiAwarenessContext.restype = ctypes.c_int
        ctx = user32.GetThreadDpiAwarenessContext()
        aw = user32.GetAwarenessFromDpiAwarenessContext(ctypes.c_void_p(ctx))
        return {0: "unaware", 1: "system", 2: "permonitor"}.get(int(aw), "unknown")
    except Exception:
        pass
    try:
        aw = ctypes.c_int()
        hproc = kernel32.GetCurrentProcess()
        if ctypes.windll.shcore.GetProcessDpiAwareness(hproc, ctypes.byref(aw)) == 0:
            return {0: "unaware", 1: "system", 2: "permonitor"}.get(aw.value, "unknown")
    except Exception:
        pass
    return "unknown"


def setup_dpi() -> str:
    """切成 Per-Monitor V2 DPI 感知，并返回真正生效的模式。

    只有 per-monitor 感知时，GetSystemMetrics / 截图 / SetCursorPos 才都是
    物理像素；否则 Windows 会按“系统 DPI”把不同缩放比例的显示器内容拉伸，
    坐标虽然自洽，但气泡的像素尺寸会跟实际分辨率对不上。
    """
    for ctx in (DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2, DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE):
        try:
            fn = user32.SetProcessDpiAwarenessContext
            fn.restype = wt.BOOL
            fn.argtypes = [ctypes.c_void_p]
            if fn(ctypes.c_void_p(ctx)):
                break
        except Exception:
            pass
    else:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            try:
                user32.SetProcessDPIAware()
            except Exception:
                pass
    return dpi_awareness()


def system_scale() -> float:
    """系统（主屏）缩放比例。"""
    try:
        dpi = int(user32.GetDpiForSystem())
        if dpi > 96:
            return max(0.5, dpi / 96.0)
    except Exception:
        pass
    # 进程不是 per-monitor 感知时，这些 API 都会被虚拟化成 96，退回注册表里的系统 DPI
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Control Panel\Desktop") as k:
            dpi = int(winreg.QueryValueEx(k, "LogPixels")[0])
        if dpi > 0:
            return max(0.5, dpi / 96.0)
    except Exception:
        pass
    return 1.0


def render_scale(mon: Monitor, mode: str | None = None) -> float:
    """桌宠界面在这块显示器上的渲染缩放。

    桌宠自己是 per-monitor 感知的，界面按所在显示器的 DPI 绘制；
    如果本进程不是 per-monitor 感知，截到的是被系统按系统 DPI 拉伸过的位图，
    此时气泡像素尺寸只跟系统缩放有关。
    """
    mode = mode or dpi_awareness()
    if mode == "permonitor":
        return mon.scale
    return system_scale()


@dataclass
class ScreenGrab:
    """整块虚拟桌面的截图，以及图像坐标 → 屏幕坐标的换算。"""

    bgr: np.ndarray
    left: int
    top: int

    def to_screen(self, x: float, y: float) -> tuple[int, int]:
        return int(round(self.left + x)), int(round(self.top + y))

    def to_image(self, x: float, y: float) -> tuple[int, int]:
        return int(round(x - self.left)), int(round(y - self.top))

    @property
    def rect(self) -> tuple[int, int, int, int]:
        h, w = self.bgr.shape[:2]
        return self.left, self.top, self.left + w, self.top + h


def virtual_screen() -> tuple[int, int, int, int]:
    """虚拟桌面（所有显示器并集）的物理矩形。"""
    vx = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
    vy = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
    vw = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
    vh = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
    if vw <= 0 or vh <= 0:
        mons = list_monitors()
        vx = min(m.left for m in mons)
        vy = min(m.top for m in mons)
        vw = max(m.right for m in mons) - vx
        vh = max(m.bottom for m in mons) - vy
    return vx, vy, vw, vh


def grab_desktop() -> ScreenGrab:
    """截取整个虚拟桌面，并保证「图像坐标 + 偏移 = 屏幕物理坐标」。"""
    img = ImageGrab.grab(all_screens=True)
    bgr = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)
    vx, vy, vw, vh = virtual_screen()
    h, w = bgr.shape[:2]
    if vw > 0 and vh > 0 and (w != vw or h != vh):
        # 少数情况下（DPI 虚拟化 / 缩放策略不同）抓到的位图尺寸和系统报告的不一致，
        # 统一缩放到虚拟桌面尺寸，保证后续换算始终是 1 像素 = 1 屏幕坐标。
        bgr = cv2.resize(bgr, (vw, vh), interpolation=cv2.INTER_LINEAR)
    return ScreenGrab(bgr=bgr, left=vx, top=vy)


def crop_monitor(grab: ScreenGrab, mon: Monitor) -> tuple[np.ndarray, int, int]:
    """从整屏截图里裁出一块显示器，返回 (图像, 图像内偏移 x, y)。"""
    img_h, img_w = grab.bgr.shape[:2]
    x0 = max(0, mon.left - grab.left)
    y0 = max(0, mon.top - grab.top)
    x1 = min(img_w, mon.right - grab.left)
    y1 = min(img_h, mon.bottom - grab.top)
    if x1 <= x0 or y1 <= y0:
        return np.empty((0, 0, 3), np.uint8), 0, 0
    return grab.bgr[y0:y1, x0:x1], x0, y0


def describe_displays() -> str:
    mons = list_monitors()
    mode = dpi_awareness()
    body = " ".join(str(m) for m in mons)
    warn = ""
    if mode != "permonitor":
        warn = "（非 Per-Monitor DPI 感知，跨不同缩放的显示器时识别可能不准）"
    return f"DPI 感知={mode}{warn}，显示器 {len(mons)} 块：{body}"


def is_admin() -> bool:
    try:
        return bool(shell32.IsUserAnAdmin())
    except Exception:
        return False


def run_as_admin() -> bool:
    try:
        params = " ".join(f'"{a}"' for a in sys.argv[1:])
        rc = shell32.ShellExecuteW(None, "runas", sys.executable, params, str(Path.cwd()), 1)
        return rc > 32
    except Exception:
        return False


_MUTEX = None


def acquire_single_instance() -> bool:
    global _MUTEX
    try:
        _MUTEX = kernel32.CreateMutexW(None, False, "Global\\WuwaTappoAutoChest_SingleInstance")
        return kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS
    except Exception:
        return True


STATS_PATH = BASE_DIR / "stats.json"


def load_total() -> int:
    try:
        return int(json.loads(STATS_PATH.read_text(encoding="utf-8")).get("total_claimed", 0))
    except Exception:
        return 0


def save_total(total: int) -> None:
    try:
        STATS_PATH.write_text(json.dumps({"total_claimed": int(total)}, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


@dataclass
class UiState:
    enabled: bool = True
    started: bool = False
    claimed: int = 0
    total: int = 0
    status: str = "监控中"
    next_scan_at: float = 0.0
    last_scan_at: float = 0.0
    preview_ts: float = 0.0
    preview_count: int = 0
    logs: deque = field(default_factory=lambda: deque(maxlen=120))
    lock: threading.RLock = field(default_factory=threading.RLock)


STATE = UiState(total=load_total())


def log(msg: str, level: str = "info") -> None:
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    try:
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > 2 * 1024 * 1024:
            LOG_PATH.write_text("", encoding="utf-8")
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    try:
        print(line, flush=True)
    except Exception:
        pass
    try:
        with STATE.lock:
            STATE.logs.append({
                "t": datetime.now().strftime("%H:%M:%S"),
                "level": level,
                "msg": msg,
            })
    except Exception:
        pass


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        if CONFIG_PATH.exists():
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                cfg.update(data)
    except Exception as e:
        log(f"配置读取失败，使用默认值: {e}")
    return cfg


def save_config(cfg: dict) -> None:
    try:
        CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        log(f"配置保存失败: {e}")


@dataclass
class Chest:
    """宝箱候选，坐标是屏幕物理坐标。"""

    x: int
    y: int
    w: int
    h: int
    score: float

    @property
    def cx(self) -> int:
        return self.x + self.w // 2

    @property
    def cy(self) -> int:
        return self.y + self.h // 2


# ---------------------------------------------------------------- 识别


def _best_cut(profile: np.ndarray, lo_frac: float = 0.2, hi_frac: float = 0.8) -> int:
    """在一维投影里找“细腰”（两个气泡之间的缝隙），返回切割位置，找不到返回 0。"""
    n = int(profile.size)
    if n < 16:
        return 0
    lo = int(n * lo_frac)
    hi = int(n * hi_frac)
    if hi - lo < 6:
        return 0
    seg = profile[lo:hi].astype(np.float32)
    k = max(3, n // 24)
    smooth = np.convolve(seg, np.ones(k, np.float32) / k, mode="same")
    idx = int(np.argmin(smooth))
    peak = float(smooth.max())
    if peak <= 0:
        return 0
    if smooth[idx] > peak * 0.85:  # 没有明显低谷，说明是一整块
        return 0
    return lo + idx


def _candidate_boxes(mask: np.ndarray, x: int, y: int, w: int, h: int, scale: float) -> list[tuple[int, int, int, int]]:
    """给一个掩膜连通块生成候选窗口；上下/左右粘连的两个气泡会拆成两个。"""
    boxes = [(x, y, x + w, y + h)]
    tall = h >= max(26 * scale, w * 1.25)
    wide = w >= max(70 * scale, h * 2.6)
    if tall:
        cut = _best_cut((mask[y:y + h, x:x + w] > 0).sum(axis=1))
        if cut:
            boxes += [(x, y, x + w, y + cut), (x, y + cut, x + w, y + h)]
    elif wide:
        cut = _best_cut((mask[y:y + h, x:x + w] > 0).sum(axis=0))
        if cut:
            boxes += [(x, y, x + cut, y + h), (x + cut, y, x + w, y + h)]
    return boxes


def _chest_icons(brown_roi: np.ndarray, min_area: float,
                 aspect_lo: float, aspect_hi: float) -> list[tuple[int, int, int, int, float]]:
    """列出棕色掩膜里像“宝箱图案”的连通块（面积够大、横向方块）。"""
    if brown_roi.size == 0:
        return []
    contours, _ = cv2.findContours(brown_roi, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    out: list[tuple[int, int, int, int, float]] = []
    for c in contours:
        area = cv2.contourArea(c)
        if area < min_area:
            continue
        ix, iy, iw, ih = cv2.boundingRect(c)
        if ih < 4 or iw < 6:
            continue
        aspect = iw / float(ih)
        if not (aspect_lo <= aspect <= aspect_hi):
            continue
        out.append((ix, iy, iw, ih, area))
    out.sort(key=lambda t: -t[4])
    return out


def _icon_window(cx: float, cy: float, iw: int, ih: int,
                 img_w: int, img_h: int) -> tuple[int, int, int, int]:
    """以宝箱图案为中心框出它所在的气泡边界。

    实测比例：气泡约是图案的 2.0 倍宽、2.9 倍高，图案居中。
    这里只取中间一段，既能覆盖气泡主体，又不会把相邻气泡框进来。
    """
    half_w = max(6.0, iw * 0.62)
    half_h = max(6.0, ih * 1.15)
    x0 = max(0, min(img_w - 1, int(round(cx - half_w))))
    x1 = max(x0 + 1, min(img_w, int(round(cx + half_w))))
    y0 = max(0, min(img_h - 1, int(round(cy - half_h))))
    y1 = max(y0 + 1, min(img_h, int(round(cy + half_h))))
    return x0, y0, x1, y1


def _cream_ratio(cream: np.ndarray, x0: int, y0: int, x1: int, y1: int) -> float:
    sub = cream[y0:y1, x0:x1]
    if sub.size == 0:
        return 0.0
    return float((sub > 0).sum()) / float(sub.size)


def find_chests(img: np.ndarray, cfg: dict, scale: float = 1.0) -> list[Chest]:
    """在 img（一块显示器的截图）里找宝箱气泡，返回 img 内坐标。

    cfg 里的尺寸阈值以 96 DPI 为基准，这里按 scale（该显示器的缩放比例）换算，
    换到 125% / 150% 的屏幕上尺寸判定口径保持一致。
    """
    s = max(0.1, float(scale))
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    cream = cv2.inRange(hsv, tuple(cfg["hsv_cream_low"]), tuple(cfg["hsv_cream_high"]))
    brown = cv2.inRange(hsv, tuple(cfg["hsv_brown_low"]), tuple(cfg["hsv_brown_high"]))
    cream = cv2.morphologyEx(cream, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    # 桌宠气泡常被壁纸上的浅色细线/发丝“粘”在一起，先做一次开运算剪掉细连接，
    # 否则两个气泡会被并成一个过大的外框，进而只能认到一个宝箱。
    open_k = max(3, int(round(2.5 * s)) | 1)
    cream = cv2.morphologyEx(cream, cv2.MORPH_OPEN, np.ones((open_k, open_k), np.uint8))

    min_area = float(cfg["min_area"]) * s * s
    max_area = float(cfg["max_area"]) * s * s
    min_w = float(cfg["min_wh"][0]) * s
    min_h = float(cfg["min_wh"][1]) * s
    max_w = float(cfg["max_wh"][0]) * s
    max_h = float(cfg["max_wh"][1]) * s
    brown_px_min = float(cfg["brown_px_min"]) * s * s
    brown_block_min = float(cfg["brown_block_min"]) * s * s
    brown_ratio_min = float(cfg.get("brown_ratio_min", 0.12))
    brown_solid_ratio_min = float(cfg.get("brown_solid_ratio_min", 0.35))
    icon_aspect_min = float(cfg.get("icon_aspect_min", 1.25))
    icon_aspect_max = float(cfg.get("icon_aspect_max", 3.0))
    icon_w_ratio_min = float(cfg.get("icon_w_ratio_min", 0.30))
    icon_h_ratio_max = float(cfg.get("icon_h_ratio_max", 0.65))
    icon_offset_max = float(cfg.get("icon_offset_max", 0.25))
    aspect_min = float(cfg.get("aspect_min", 1.05))
    aspect_max = float(cfg.get("aspect_max", 2.2))
    fill_min = float(cfg.get("fill_min", 0.50))
    fill_max = float(cfg.get("fill_max", 0.95))
    clipped_min_ratio = float(cfg.get("clipped_min_ratio", 0.45))
    clipped_aspect_min = float(cfg.get("clipped_aspect_min", 0.45))
    clipped_fill_min = float(cfg.get("clipped_fill_min", 0.35))
    icon_cream_ratio_min = float(cfg.get("icon_cream_ratio_min", 0.30))

    img_h, img_w = img.shape[:2]
    contours, _ = cv2.findContours(cream, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    found: list[Chest] = []

    for c in contours:
        x, y, w, h = cv2.boundingRect(c)
        # 桌宠贴着屏幕边缘时，气泡会被屏幕边界裁掉一块：这类候选放宽尺寸下限，
        # 后面再按“贴边”放宽长宽比/填充率，否则靠边放桌宠就永远认不到。
        at_edge = x <= 2 or y <= 2 or x + w >= img_w - 2 or y + h >= img_h - 2
        lo_w = min_w * (clipped_min_ratio if at_edge else 0.85)
        lo_h = min_h * (clipped_min_ratio if at_edge else 0.85)
        if w < lo_w or h < lo_h:
            continue
        if w > max_w * 2.6 or h > max_h * 2.6:
            continue
        for bx0, by0, bx1, by1 in _candidate_boxes(cream, x, y, w, h, s):
            bx0 = max(0, bx0)
            by0 = max(0, by0)
            bx1 = min(img_w, bx1)
            by1 = min(img_h, by1)
            sub = cream[by0:by1, bx0:bx1]
            if sub.size == 0:
                continue
            sub_contours, _ = cv2.findContours(sub, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            if not sub_contours:
                continue
            best = max(sub_contours, key=cv2.contourArea)
            area = cv2.contourArea(best)
            rx, ry, rw, rh = cv2.boundingRect(best)
            gx, gy = bx0 + rx, by0 + ry

            # 两个宝箱气泡贴在一起（还常被屏幕边缘裁掉一块）时会连成一个整块，
            # 整块的长宽比/面积都不像单个气泡。这时改用“宝箱图案”作锚点：
            # 一块里出现两个图案就说明是两个气泡，各自出一个候选。
            icons = _chest_icons(brown[gy:gy + rh, gx:gx + rw], brown_block_min,
                                 icon_aspect_min * 0.7, icon_aspect_max * 1.6)
            if len(icons) >= 2:
                for icx, icy, icw, ich, ic_area in icons:
                    cx_img = gx + icx + icw / 2.0
                    cy_img = gy + icy + ich / 2.0
                    wx0, wy0, wx1, wy1 = _icon_window(cx_img, cy_img, icw, ich, img_w, img_h)
                    if _cream_ratio(cream, wx0, wy0, wx1, wy1) < icon_cream_ratio_min:
                        continue
                    found.append(Chest(wx0, wy0, wx1 - wx0, wy1 - wy0,
                                       ic_area / float(max((wx1 - wx0) * (wy1 - wy0), 1))))
                continue
            clipped = gx <= 2 or gy <= 2 or gx + rw >= img_w - 2 or gy + rh >= img_h - 2
            if clipped:
                if rw < min_w * clipped_min_ratio or rh < min_h * clipped_min_ratio:
                    continue
            elif rw < min_w or rh < min_h:
                continue
            if rw > max_w or rh > max_h:
                continue
            if not (min_area * (clipped_min_ratio ** 2 if clipped else 1.0) <= area <= max_area):
                continue
            aspect = rw / max(rh, 1)
            if not ((clipped_aspect_min if clipped else aspect_min) <= aspect <= aspect_max):
                continue
            fill = area / float(rw * rh)
            if not ((clipped_fill_min if clipped else fill_min) <= fill <= fill_max):
                continue
            roi = brown[gy:gy + rh, gx:gx + rw]
            if roi.size == 0:
                continue
            brown_px = int((roi > 0).sum())
            if brown_px < brown_px_min:
                continue
            bcontours, _ = cv2.findContours(roi, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            if not bcontours:
                continue
            icon = max(bcontours, key=cv2.contourArea)
            solid = cv2.contourArea(icon)
            if solid < brown_block_min:
                continue
            # 宝箱图案要“成块”、占气泡面积比例够大，且本身是横向的方块并大致居中；
            # 壁纸里的棕色发丝/色块通常细长、偏方或不在中心。
            if brown_px / max(area, 1.0) < brown_ratio_min:
                continue
            if solid / max(brown_px, 1.0) < brown_solid_ratio_min:
                continue
            ix, iy, iw, ih = cv2.boundingRect(icon)
            icon_aspect = iw / float(max(ih, 1))
            # 被屏幕边缘裁掉一块时，图案本身也被切了，几何判据要相应放宽
            a_lo = icon_aspect_min * (0.7 if clipped else 1.0)
            a_hi = icon_aspect_max * (1.6 if clipped else 1.0)
            if not (a_lo <= icon_aspect <= a_hi):
                continue
            w_lo = icon_w_ratio_min * (0.6 if clipped else 1.0)
            if iw / float(rw) < w_lo or ih / float(rh) > icon_h_ratio_max:
                continue
            off_max = icon_offset_max * (1.8 if clipped else 1.0)
            off_x = abs((ix + iw / 2.0) - rw / 2.0) / float(max(rw, 1))
            off_y = abs((iy + ih / 2.0) - rh / 2.0) / float(max(rh, 1))
            if off_x > off_max or off_y > off_max:
                continue
            found.append(Chest(gx, gy, rw, rh, brown_px / float(rw * rh)))

    # 去重：同一气泡被拆成多块时，只保留最外层的那个框（面积大的），
    # 但上下相邻的两个真气泡（相距 70px 左右）必须都保留。
    found.sort(key=lambda m: (-m.w * m.h, m.y, m.x))
    keep: list[Chest] = []
    for m in found:
        dup = False
        for o in keep:
            near = (abs(m.cx - o.cx) <= max(4.0, 0.25 * min(m.w, o.w))
                    and abs(m.cy - o.cy) <= max(4.0, 0.25 * min(m.h, o.h)))
            inside = o.x <= m.cx <= o.x + o.w and o.y <= m.cy <= o.y + o.h
            if near or inside:
                dup = True
                break
        if not dup:
            keep.append(m)
    keep.sort(key=lambda m: (m.y, m.x))
    return keep


def get_process_name(pid: int) -> str:
    if not pid:
        return ""
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        size = wt.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value).lower()
        return ""
    finally:
        kernel32.CloseHandle(h)


def window_at(x: int, y: int) -> tuple[str, str, int, int]:
    """返回该屏幕点最上层窗口的 (类名, 进程名, pid, 顶层窗口句柄)。"""
    pt = wt.POINT(int(x), int(y))
    hwnd = user32.WindowFromPoint(pt)
    if not hwnd:
        return "", "", 0, 0
    root = user32.GetAncestor(hwnd, 2) or hwnd  # GA_ROOT
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(root, ctypes.byref(pid))
    cls = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, cls, 256)
    return cls.value, get_process_name(pid.value), pid.value, int(root)


def pet_overlay_rects() -> list[tuple[int, int, int, int]]:
    """桌宠桌面覆盖窗口（DesktopCatInput）的矩形，用于限定识别/点击范围。"""
    rects: list[tuple[int, int, int, int]] = []
    proc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def _cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        cls = ctypes.create_unicode_buffer(128)
        user32.GetClassNameW(hwnd, cls, 128)
        if cls.value not in PET_OVERLAY_CLASSES:
            return True
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if get_process_name(pid.value) != PET_PROCESS:
            return True
        r = wt.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(r)):
            return True
        if r.right > r.left and r.bottom > r.top:
            rects.append((r.left, r.top, r.right, r.bottom))
        return True

    try:
        user32.EnumWindows(proc(_cb), 0)
    except Exception:
        pass
    return rects


def inside_any(rects: list[tuple[int, int, int, int]], x: int, y: int) -> bool:
    return any(r[0] <= x < r[2] and r[1] <= y < r[3] for r in rects)


def is_pet_window(x: int, y: int, overlays: list[tuple[int, int, int, int]] | None = None) -> tuple[bool, str]:
    """点击前的硬安全锁：该点必须是桌宠进程的窗口，且在桌面覆盖窗口范围内。

    这里不再只认 DesktopCatInput 一个类名：桌宠是 Tauri 应用，Toast / 设置面板
    等同样是 wuwatappo.exe 的窗口，必要时会挡在覆盖窗口上面；
    只要进程对、点落在覆盖窗口里，点击就是安全的。
    """
    cls, proc, pid, root = window_at(x, y)
    info = f"class={cls or '?'} proc={proc or '?'} pid={pid}"
    if proc != PET_PROCESS:
        return False, info
    if overlays:
        if not inside_any(overlays, x, y):
            return False, info + " 不在桌宠覆盖窗口内"
        return True, info
    if cls not in PET_WINDOW_CLASSES:
        return False, info
    return True, info


def click_at(x: int, y: int) -> None:
    old = wt.POINT()
    user32.GetCursorPos(ctypes.byref(old))
    user32.SetCursorPos(x, y)
    time.sleep(0.12)
    user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.04)
    user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    time.sleep(0.12)
    user32.SetCursorPos(old.x, old.y)


def annotate(img: np.ndarray, chests: list[Chest], path: Path, grab: ScreenGrab | None = None) -> np.ndarray:
    out = img.copy()
    for m in chests:
        x0, y0 = (m.x - grab.left, m.y - grab.top) if grab else (m.x, m.y)
        x1, y1 = x0 + m.w, y0 + m.h
        cv2.rectangle(out, (x0, y0), (x1, y1), (0, 220, 120), 2)
        cv2.putText(
            out, f"{m.score:.2f}", (x0, max(12, y0 - 6)),
            cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 220, 120), 1, cv2.LINE_AA,
        )
    cv2.imwrite(str(path), out)
    return out


@dataclass
class DetectResult:
    grab: ScreenGrab
    candidates: list[Chest]   # 屏幕坐标，未做窗口过滤
    chests: list[Chest]       # 屏幕坐标，已确认属于桌宠窗口


def detect(cfg: dict, pet_only: bool = True) -> DetectResult:
    """截屏 + 逐显示器识别，返回屏幕坐标的候选/有效宝箱。"""
    grab = grab_desktop()
    mode = dpi_awareness()
    monitors = list_monitors()
    overlays = pet_overlay_rects() if pet_only else []

    # 只在桌宠覆盖窗口所在的显示器上找，避免其他屏幕上的相似色块误判、也让识别更快
    targets = [m for m in monitors if overlaps(m, overlays)] if overlays else monitors

    candidates: list[Chest] = []
    for mon in targets:
        roi, ox, oy = crop_monitor(grab, mon)
        if roi.size == 0:
            continue
        for m in find_chests(roi, cfg, render_scale(mon, mode)):
            candidates.append(Chest(m.x + ox + grab.left, m.y + oy + grab.top, m.w, m.h, m.score))

    chests: list[Chest] = []
    for m in candidates:
        ok, _info = is_pet_window(m.cx, m.cy, overlays or None)
        if ok:
            chests.append(m)
    return DetectResult(grab=grab, candidates=candidates, chests=chests)


def overlaps(mon: Monitor, rects: list[tuple[int, int, int, int]]) -> bool:
    return any(
        mon.left < r[2] and mon.right > r[0] and mon.top < r[3] and mon.bottom > r[1]
        for r in rects
    )


def scan(cfg: dict, save_preview: bool = False, pet_only: bool = True) -> tuple[list[Chest], ScreenGrab]:
    res = detect(cfg, pet_only=pet_only)
    if save_preview:
        annotate(res.grab.bgr, res.chests, BASE_DIR / "detect_preview.png", res.grab)
    return res.chests, res.grab


def _chest_still(cfg: dict, chest: Chest) -> bool:
    s = render_scale(monitor_at(chest.cx, chest.cy))
    tol = max(6, int(round(8 * s)))
    after, _ = scan(cfg)
    return any(abs(a.cx - chest.cx) <= tol and abs(a.cy - chest.cy) <= tol for a in after)


def monitor_at(x: int, y: int) -> Monitor:
    """屏幕点所在的显示器。"""
    mons = list_monitors()
    pt = wt.POINT(int(x), int(y))
    hmon = int(user32.MonitorFromPoint(pt, MONITOR_DEFAULTTONEAREST) or 0)
    for m in mons:
        if m.handle and m.handle == hmon:
            return m
    for m in mons:
        if m.left <= x < m.right and m.top <= y < m.bottom:
            return m
    return mons[0] if mons else Monitor(0, 0, 0, 1920, 1080, 96, True)


def cursor_monitor() -> Monitor:
    """鼠标当前所在的显示器（用于把界面放到用户正在用的那块屏上）。"""
    pt = wt.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return monitor_at(pt.x, pt.y)


def _wait_chest_gone(cfg: dict, chest: Chest, timeout: float | None = None) -> bool:
    """点击后轮询图标是否消失。

    领取动画有时要 2~3 秒，固定等 2.2 秒再判一次容易把成功当失败。
    """
    if timeout is None:
        timeout = float(cfg.get("verify_timeout_sec", 3.0))
    deadline = time.time() + max(0.0, timeout)
    while True:
        if not _chest_still(cfg, chest):
            return True
        if time.time() >= deadline:
            return False
        time.sleep(0.5)


def _click_and_verify(cfg: dict, chest: Chest) -> bool:
    tries = 1 + max(0, int(cfg.get("retry_per_chest", 1)))
    for i in range(tries):
        if i == 0:
            log(f"发现宝箱 ({chest.cx},{chest.cy})，点击领取", "ok")
        else:
            log("宝箱仍在，重试一次", "warn")
        click_at(chest.cx, chest.cy)
        time.sleep(float(cfg["click_wait_sec"]))
        if _wait_chest_gone(cfg, chest):
            return True
    return False


def claim_once(cfg: dict, dry_run: bool = False, verbose: bool = True) -> int:
    """领取本轮能看到的宝箱；某个气泡点不掉时会继续尝试下一个，而不是整轮放弃。"""
    claimed = 0
    max_clicks = max(1, int(cfg["max_clicks_per_round"]))
    failed: list[tuple[int, int]] = []

    for _ in range(max_clicks):
        chests, _grab = scan(cfg)
        chests = [
            m for m in chests
            if all(abs(m.cx - fx) > 16 or abs(m.cy - fy) > 16 for fx, fy in failed)
        ]
        if not chests:
            break

        target = chests[0]
        if dry_run:
            log(f"[预览] 发现宝箱 ({target.cx},{target.cy}) 尺寸 {target.w}x{target.h}", "ok")
            claimed += 1
            failed.append((target.cx, target.cy))
            continue

        if _click_and_verify(cfg, target):
            claimed += 1
            log("领取成功", "ok")
            time.sleep(0.6)
        else:
            failed.append((target.cx, target.cy))
            log("本个宝箱未领取成功，继续检查其它气泡", "warn")

    return claimed


def monitor_loop(cfg: dict, stop_event: threading.Event, enabled: threading.Event, on_claim=None) -> None:
    last_fail = 0.0
    while not stop_event.is_set():
        if not enabled.is_set():
            STATE.status = "已暂停" if STATE.started else "未启动"
            STATE.next_scan_at = 0
            stop_event.wait(0.5)
            continue
        try:
            if time.time() - last_fail < float(cfg["fail_cooldown_sec"]):
                STATE.status = "冷却中"
                stop_event.wait(1.0)
                continue

            interval = float(cfg["interval_sec"])
            STATE.status = "检查中"
            STATE.next_scan_at = time.time() + interval
            STATE.last_scan_at = time.time()

            chests, _grab = scan(cfg)
            if not chests:
                STATE.status = "监控中"
                stop_event.wait(interval)
                continue

            STATE.status = "领取中"
            got = claim_once(cfg, dry_run=False, verbose=True)
            if got:
                with STATE.lock:
                    STATE.claimed += got
                    STATE.total += got
                    total = STATE.total
                save_total(total)
                STATE.status = "监控中"
                if on_claim:
                    on_claim(got)
                stop_event.wait(1.5)
            else:
                last_fail = time.time()
                STATE.status = "冷却中"
                log(f"本轮未领取，冷却 {cfg['fail_cooldown_sec']} 秒", "warn")
                stop_event.wait(2.0)
        except Exception as e:
            STATE.status = "异常"
            log(f"监控异常: {e}\n{traceback.format_exc(limit=2)}", "err")
            stop_event.wait(5.0)
    STATE.status = "已停止"
    STATE.next_scan_at = 0


def make_icon_image() -> Image.Image:
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((4, 4, 60, 60), radius=14, fill=(250, 214, 160, 255), outline=(140, 90, 40, 255), width=2)
    d.rounded_rectangle((16, 26, 48, 46), radius=4, fill=(139, 94, 60, 255))
    d.rectangle((16, 30, 48, 34), fill=(90, 58, 36, 255))
    d.rectangle((30, 32, 34, 42), fill=(250, 214, 160, 255))
    d.ellipse((24, 12, 40, 26), fill=(250, 214, 160, 255), outline=(140, 90, 40, 255))
    return img


def run_tray(cfg: dict, auto_stop_sec: float = 0) -> None:
    import pystray
    from pystray import Menu, MenuItem

    stop_event = threading.Event()
    enabled = threading.Event()
    enabled.set()
    state = {"status": "运行中", "claimed": 0}

    def on_claim(n: int) -> None:
        state["claimed"] += n
        state["status"] = f"运行中 · 已领取 {state['claimed']}"
        try:
            icon.notify(f"已领取 {n} 个宝箱", "WuwaTappo 自动开箱")
        except Exception:
            pass

    def toggle(icon_, item):
        if enabled.is_set():
            enabled.clear()
            state["status"] = "已暂停"
        else:
            enabled.set()
            state["status"] = "运行中"
        icon_.update_menu()

    def test(icon_, item):
        try:
            chests, grab = scan(cfg, save_preview=True)
            if chests:
                lines = [f"({m.cx},{m.cy}) {m.w}x{m.h}" for m in chests]
                msg = "发现 %d 个宝箱:\n%s\n预览图已保存" % (len(chests), "\n".join(lines))
            else:
                msg = "当前没有识别到宝箱。\n预览图已保存，可查看识别结果。"
            icon.notify(msg, "识别测试")
            try:
                os.startfile(str(BASE_DIR / "detect_preview.png"))
            except Exception:
                pass
        except Exception as e:
            icon.notify(f"识别失败: {e}", "识别测试")

    def open_log(icon_, item):
        try:
            os.startfile(str(LOG_PATH))
        except Exception:
            pass

    def open_folder(icon_, item):
        try:
            os.startfile(str(BASE_DIR))
        except Exception:
            pass

    def quit_app(icon_, item):
        stop_event.set()
        icon.stop()

    def status_text(item):
        return f"状态：{state['status']}"

    icon = pystray.Icon(
        APP_NAME,
        make_icon_image(),
        "WuwaTappo 自动开箱",
        menu=Menu(
            MenuItem(status_text, None, enabled=False),
            MenuItem("暂停 / 继续", toggle),
            MenuItem("识别测试", test),
            MenuItem("打开日志", open_log),
            MenuItem("打开数据目录", open_folder),
            Menu.SEPARATOR,
            MenuItem("退出", quit_app),
        ),
    )

    thread = threading.Thread(
        target=monitor_loop,
        args=(cfg, stop_event, enabled, on_claim),
        daemon=True,
    )
    thread.start()
    log("托盘程序启动")
    log(describe_displays())
    if auto_stop_sec:
        threading.Timer(auto_stop_sec, icon.stop).start()
    else:
        def _hello():
            try:
                icon.notify("已启动。检测到宝箱会自动点击领取。", "WuwaTappo 自动开箱")
            except Exception:
                pass
        threading.Timer(1.5, _hello).start()
    icon.run()
    stop_event.set()
    thread.join(timeout=2)
    log("托盘程序退出")


def main() -> int:
    mode = setup_dpi()
    parser = argparse.ArgumentParser(description="WuwaTappo 自动开箱助手")
    parser.add_argument("--tray", action="store_true", help="使用旧托盘模式（备用）")
    parser.add_argument("--ui", action="store_true", help="直接打开浏览器详细面板")
    parser.add_argument("--once", action="store_true", help="只扫描一轮，领取一个宝箱后退出")
    parser.add_argument("--dry-run", action="store_true", help="只识别不点击")
    parser.add_argument("--preview", action="store_true", help="保存识别预览图后退出")
    parser.add_argument("--displays", action="store_true", help="打印显示器/DPI 信息后退出")
    parser.add_argument("--selftest", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()

    cfg = load_config()
    save_config(cfg)

    if args.displays:
        log(describe_displays())
        return 0

    if args.preview:
        chests, grab = scan(cfg, save_preview=True)
        log(f"识别到 {len(chests)} 个宝箱（虚拟桌面 {grab.rect}），预览图: {BASE_DIR / 'detect_preview.png'}")
        try:
            os.startfile(str(BASE_DIR / "detect_preview.png"))
        except Exception:
            pass
        return 0

    if args.once:
        if not args.dry_run and not is_admin():
            log("需要管理员权限（桌宠以管理员运行），请用管理员身份启动。")
            return 2
        n = claim_once(cfg, dry_run=args.dry_run)
        log(f"本轮领取 {n} 个")
        return 0

    if not is_admin():
        log("需要管理员权限：桌宠以管理员运行，本工具也必须以管理员启动。")
        try:
            ctypes.windll.user32.MessageBoxW(
                None,
                "WuwaTappo 以管理员权限运行，自动开箱工具也需要管理员权限。\n\n"
                "请右键“以管理员身份运行”，或使用打包版的 exe。",
                "需要管理员权限",
                0x30,
            )
        except Exception:
            pass
        return 2

    if not acquire_single_instance():
        try:
            user32.MessageBoxW(None, "WuwaTappo 自动开箱已经在运行。", APP_NAME, 0x40)
        except Exception:
            pass
        return 0

    if mode != "permonitor":
        log(f"提示：当前 DPI 感知模式为 {mode}，多显示器不同缩放时识别可能不准。", "warn")
    log(describe_displays())

    if args.tray:
        run_tray(cfg, auto_stop_sec=6 if args.selftest else 0)
    elif args.ui:
        from ui_server import run_ui
        try:
            run_ui(cfg, auto_stop_sec=8 if args.selftest else 0)
        except BaseException:
            log("界面异常:\n" + traceback.format_exc(), "err")
            return 3
    else:
        from mini_ui import run_mini
        try:
            run_mini(cfg, auto_stop_sec=8 if args.selftest else 0)
        except BaseException:
            log("界面异常:\n" + traceback.format_exc(), "err")
            return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
