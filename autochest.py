#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WuwaTappo 自动开箱助手

原理：屏幕取色 + 轮廓识别，找到桌宠右侧的“宝箱气泡”，
校验该坐标处的窗口确实属于 WuwaTappo 后，模拟一次鼠标左键点击。

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
VERSION = "0.2.0"

BASE_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / APP_NAME
BASE_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_PATH = BASE_DIR / "config.json"
LOG_PATH = BASE_DIR / "autochest.log"

DEFAULT_CONFIG = {
    "interval_sec": 15,
    "auto_start": True,
    "click_wait_sec": 2.2,
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
}

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
shell32 = ctypes.windll.shell32

SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79

MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

# 64 位下必须显式声明指针/句柄返回值，避免 ctypes 默认按 int 截断。
user32.WindowFromPoint.restype = wt.HWND
user32.WindowFromPoint.argtypes = [wt.POINT]
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
user32.GetCursorPos.argtypes = [ctypes.POINTER(wt.POINT)]
user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
kernel32.OpenProcess.restype = wt.HANDLE
kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
kernel32.CreateMutexW.restype = wt.HANDLE
kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.LPCWSTR]
kernel32.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
kernel32.CloseHandle.argtypes = [wt.HANDLE]


def setup_dpi() -> None:
    try:
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            user32.SetProcessDPIAware()


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


def grab_bgr() -> tuple[np.ndarray, int, int]:
    img = ImageGrab.grab(all_screens=True)
    arr = np.array(img)
    bgr = cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
    vx = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
    vy = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
    return bgr, vx, vy


def find_chests(img: np.ndarray, cfg: dict) -> list[Chest]:
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    cream = cv2.inRange(
        hsv,
        tuple(cfg["hsv_cream_low"]),
        tuple(cfg["hsv_cream_high"]),
    )
    brown = cv2.inRange(
        hsv,
        tuple(cfg["hsv_brown_low"]),
        tuple(cfg["hsv_brown_high"]),
    )
    cream = cv2.morphologyEx(cream, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))

    contours, _ = cv2.findContours(cream, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    found: list[Chest] = []
    for c in contours:
        x, y, w, h = cv2.boundingRect(c)
        area = cv2.contourArea(c)
        if not (cfg["min_area"] <= area <= cfg["max_area"]):
            continue
        if w < max(22, cfg["min_wh"][0]) or h < max(15, cfg["min_wh"][1]):
            continue
        if w > min(110, cfg["max_wh"][0]) or h > min(80, cfg["max_wh"][1]):
            continue
        aspect = w / max(h, 1)
        if not (1.05 <= aspect <= 2.2):
            continue
        fill = area / float(w * h)
        if not (0.40 <= fill <= 0.95):
            continue
        roi = brown[y:y + h, x:x + w]
        if roi.size == 0:
            continue
        brown_px = int((roi > 0).sum())
        if brown_px < cfg["brown_px_min"]:
            continue
        bcontours, _ = cv2.findContours(roi, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not bcontours:
            continue
        solid = max(cv2.contourArea(c2) for c2 in bcontours)
        if solid < cfg["brown_block_min"]:
            continue
        found.append(Chest(x, y, w, h, brown_px / float(w * h)))

    found.sort(key=lambda m: (m.y, m.x))
    dedup: list[Chest] = []
    for m in found:
        if all(abs(m.cx - o.cx) > 6 or abs(m.cy - o.cy) > 6 for o in dedup):
            dedup.append(m)
    return dedup


def get_process_name(pid: int) -> str:
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


def window_at(x: int, y: int) -> tuple[str, str, int]:
    pt = wt.POINT(x, y)
    hwnd = user32.WindowFromPoint(pt)
    if not hwnd:
        return "", "", 0
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    cls = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, cls, 256)
    return cls.value, get_process_name(pid.value), pid.value


def is_pet_window(x: int, y: int) -> tuple[bool, str]:
    cls, proc, pid = window_at(x, y)
    ok = cls == "DesktopCatInput" and proc == "wuwatappo.exe"
    return ok, f"class={cls or '?'} proc={proc or '?'} pid={pid}"


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


def annotate(img: np.ndarray, chests: list[Chest], path: Path) -> np.ndarray:
    out = img.copy()
    for m in chests:
        cv2.rectangle(out, (m.x, m.y), (m.x + m.w, m.y + m.h), (0, 220, 120), 2)
        cv2.putText(
            out, f"{m.score:.2f}", (m.x, max(12, m.y - 6)),
            cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 220, 120), 1, cv2.LINE_AA,
        )
    cv2.imwrite(str(path), out)
    return out


def scan(cfg: dict, save_preview: bool = False, pet_only: bool = True) -> tuple[list[Chest], int, int, np.ndarray]:
    img, vx, vy = grab_bgr()
    chests = find_chests(img, cfg)
    if pet_only:
        chests = [m for m in chests if is_pet_window(vx + m.cx, vy + m.cy)[0]]
    if save_preview:
        annotate(img, chests, BASE_DIR / "detect_preview.png")
    return chests, vx, vy, img


def claim_once(cfg: dict, dry_run: bool = False, verbose: bool = True) -> int:
    claimed = 0
    max_clicks = int(cfg["max_clicks_per_round"])
    for _ in range(max_clicks):
        chests, vx, vy, _ = scan(cfg)
        if not chests:
            break

        target = None
        for m in chests:
            sx, sy = vx + m.cx, vy + m.cy
            ok, info = is_pet_window(sx, sy)
            if ok:
                target = (m, sx, sy)
                break
            if verbose:
                log(f"跳过：坐标处不是桌宠窗口 ({sx},{sy}) {info}", "warn")

        if not target:
            if verbose:
                log("候选区域都不属于桌宠窗口，本轮跳过", "warn")
            break

        m, sx, sy = target
        if dry_run:
            log(f"[预览] 发现宝箱 ({sx},{sy}) 尺寸 {m.w}x{m.h}", "ok")
            claimed += 1
            continue

        log(f"发现宝箱 ({sx},{sy})，点击领取", "ok")
        click_at(sx, sy)
        time.sleep(float(cfg["click_wait_sec"]))

        after, _, _, _ = scan(cfg)
        still = any(abs(a.cx - m.cx) <= 8 and abs(a.cy - m.cy) <= 8 for a in after)
        if still:
            time.sleep(0.5)
            log("宝箱仍在，重试一次", "warn")
            click_at(sx, sy)
            time.sleep(float(cfg["click_wait_sec"]))
            after, _, _, _ = scan(cfg)
            still = any(abs(a.cx - m.cx) <= 8 and abs(a.cy - m.cy) <= 8 for a in after)
        if still:
            log("领取失败：宝箱图标未消失（可能字符数不足或界面状态异常）", "err")
            break

        claimed += 1
        log("领取成功", "ok")
        time.sleep(0.6)

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

            chests, vx, vy, _ = scan(cfg)
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
            chests, vx, vy, _ = scan(cfg, save_preview=True)
            if chests:
                lines = [f"({vx + m.cx},{vy + m.cy}) {m.w}x{m.h}" for m in chests]
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
    setup_dpi()
    parser = argparse.ArgumentParser(description="WuwaTappo 自动开箱助手")
    parser.add_argument("--tray", action="store_true", help="使用旧托盘模式（备用）")
    parser.add_argument("--ui", action="store_true", help="直接打开浏览器详细面板")
    parser.add_argument("--once", action="store_true", help="只扫描一轮，领取一个宝箱后退出")
    parser.add_argument("--dry-run", action="store_true", help="只识别不点击")
    parser.add_argument("--preview", action="store_true", help="保存识别预览图后退出")
    parser.add_argument("--selftest", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()

    cfg = load_config()
    save_config(cfg)

    if args.preview:
        chests, vx, vy, _ = scan(cfg, save_preview=True)
        log(f"识别到 {len(chests)} 个宝箱，预览图: {BASE_DIR / 'detect_preview.png'}")
        try:
            os.startfile(str(BASE_DIR / "detect_preview.png"))
        except Exception:
            pass
        return 0

    if args.once:
        if not is_admin():
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
