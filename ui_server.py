#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地控制面板：HTTP + Edge app 窗口。

只监听 127.0.0.1，随机端口 + 随机 token；界面是纯静态 HTML，
后台仍然由 autochest.monitor_loop 负责识别与点击。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import cv2

import autochest as ac


def _ui_dir() -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / "ui"


def find_edge() -> str | None:
    candidates = [
        os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%LocalAppData%\Microsoft\Edge\Application\msedge.exe"),
    ]
    for p in candidates:
        if p and os.path.isfile(p):
            return p
    try:
        import winreg
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe",
        ) as key:
            p = winreg.QueryValue(key, None)
            if p and os.path.isfile(p):
                return p
    except Exception:
        pass
    return None


def _clean_env() -> dict:
    """PyInstaller 会把 _MEI 临时目录塞进 PATH，Edge 可能误加载其中的 DLL。"""
    env = dict(os.environ)
    parts = [p for p in env.get("PATH", "").split(os.pathsep) if p and "_MEI" not in p.upper()]
    env["PATH"] = os.pathsep.join(parts)
    for key in ("PYTHONHOME", "PYTHONPATH", "TCL_LIBRARY", "TK_LIBRARY"):
        env.pop(key, None)
    return env


def launch_edge(port: int, token: str):
    url = f"http://127.0.0.1:{port}/?token={token}"
    edge = find_edge()
    if not edge:
        webbrowser.open(url)
        return None
    # 每次用独立 profile，避免 Edge 后台进程复用旧实例导致“进程在、窗口不出来”
    profile = ac.BASE_DIR / f"edge-profile-{int(time.time())}-{os.getpid()}"
    profile.mkdir(parents=True, exist_ok=True)
    for old in ac.BASE_DIR.glob("edge-profile-*"):
        if old != profile:
            shutil.rmtree(old, ignore_errors=True)
    args = [
        edge,
        f"--app={url}",
        "--window-size=420,680",
        "--window-position=40,190",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--force-dark-mode",
        "--disable-background-mode",
        "--disable-features=msEdgeSidebarV2",
    ]
    try:
        err = open(ac.BASE_DIR / "edge-stderr.log", "ab")
        return subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=err,
            close_fds=True,
            cwd=str(ac.BASE_DIR),
            env=_clean_env(),
        )
    except Exception:
        webbrowser.open(url)
        return None


user32 = ctypes.windll.user32


APP_TITLE = "WuwaTappo 自动开箱"


def find_app_window(title: str = APP_TITLE) -> int:
    found: list[tuple[int, int]] = []
    proc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        buf = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, buf, 256)
        if buf.value.strip() != title:
            return True
        cls = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, cls, 64)
        if cls.value != "Chrome_WidgetWin_1":
            return True
        r = wt.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        w, h = r.right - r.left, r.bottom - r.top
        if w > 200 and h > 200:
            found.append((hwnd, w * h))
        return True

    user32.EnumWindows(proc(cb), 0)
    return max(found, key=lambda x: x[1])[0] if found else 0


def focus_app_window(title: str = APP_TITLE, timeout: float = 12.0) -> int:
    deadline = time.time() + timeout
    while time.time() < deadline:
        hwnd = find_app_window(title)
        if hwnd:
            try:
                dwm = ctypes.windll.dwmapi
                on = ctypes.c_int(1)
                dwm.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(on), 4)
                cap = ctypes.c_int(0x0016110E)
                dwm.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(cap), 4)
                txt = ctypes.c_int(0x00F7EEE9)
                dwm.DwmSetWindowAttribute(hwnd, 36, ctypes.byref(txt), 4)
            except Exception:
                pass
            try:
                user32.ShowWindow(hwnd, 9)
                user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x0001 | 0x0002)
                user32.SetWindowPos(hwnd, -2, 0, 0, 0, 0, 0x0001 | 0x0002)
                user32.SetForegroundWindow(hwnd)
            except Exception:
                pass
            return hwnd
        time.sleep(0.4)
    return 0


class UiServer:
    def __init__(self, cfg: dict, stop_event: threading.Event, enabled: threading.Event):
        self.cfg = cfg
        self.stop_event = stop_event
        self.enabled = enabled
        self.token = secrets.token_urlsafe(18)
        self.httpd: ThreadingHTTPServer | None = None
        self.port = 0
        self.edge = None
        self.hwnd = 0
        self._stopped = False
        self._quitting = False
        self.on_quit = None

    # ---------- lifecycle ----------

    def start(self) -> int:
        handler = _make_handler(self)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.httpd.daemon_threads = True
        self.port = int(self.httpd.server_address[1])
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self.port

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        try:
            self.httpd.shutdown()
        except Exception:
            pass
        try:
            self.httpd.server_close()
        except Exception:
            pass

    def request_quit(self) -> None:
        if self._quitting:
            return
        self._quitting = True
        self.stop_event.set()
        if self.on_quit:
            try:
                self.on_quit()
            except Exception:
                pass
        threading.Thread(target=self._shutdown_all, daemon=True).start()

    def _shutdown_all(self) -> None:
        time.sleep(0.25)
        self.stop()
        if self.hwnd and user32.IsWindow(self.hwnd):
            try:
                user32.PostMessageW(self.hwnd, 0x0010, 0, 0)  # WM_CLOSE
            except Exception:
                pass
        if self.edge is not None and self.edge.poll() is None:
            try:
                self.edge.terminate()
            except Exception:
                pass

    # ---------- api ----------

    def state_dict(self) -> dict:
        with ac.STATE.lock:
            logs = list(ac.STATE.logs)[-34:]
        return {
            "enabled": self.enabled.is_set(),
            "status": ac.STATE.status,
            "claimed": ac.STATE.claimed,
            "total": ac.STATE.total,
            "next_scan_at": ac.STATE.next_scan_at,
            "last_scan_at": ac.STATE.last_scan_at,
            "interval": int(self.cfg.get("interval_sec", 15)),
            "auto_start": bool(self.cfg.get("auto_start", True)),
            "preview_ts": ac.STATE.preview_ts,
            "preview_count": ac.STATE.preview_count,
            "logs": logs,
        }

    def set_enabled(self, value: bool) -> None:
        if value:
            self.enabled.set()
            ac.STATE.enabled = True
            ac.STATE.started = True
            ac.STATE.status = "监控中"
            ac.STATE.next_scan_at = time.time() + float(self.cfg.get("interval_sec", 15))
            ac.log("已继续监控", "ok")
        else:
            self.enabled.clear()
            ac.STATE.enabled = False
            ac.STATE.status = "已暂停"
            ac.STATE.next_scan_at = 0
            ac.log("已暂停监控", "warn")

    def scan_now(self) -> dict:
        img, vx, vy = ac.grab_bgr()
        candidates = ac.find_chests(img, self.cfg)
        chests = [m for m in candidates if ac.is_pet_window(vx + m.cx, vy + m.cy)[0]]
        annotated = ac.annotate(img, chests, ac.BASE_DIR / "detect_preview.png")

        h, w = annotated.shape[:2]
        scale = min(1.0, 900.0 / max(w, 1))
        thumb = cv2.resize(
            annotated,
            (max(1, int(w * scale)), max(1, int(h * scale))),
            interpolation=cv2.INTER_AREA,
        )
        cv2.imwrite(str(ac.BASE_DIR / "preview.jpg"), thumb, [int(cv2.IMWRITE_JPEG_QUALITY), 84])

        ac.STATE.preview_ts = time.time()
        ac.STATE.preview_count = len(chests)
        if chests:
            ac.log(f"识别测试：{len(chests)} 个宝箱可领取（候选 {len(candidates)} 个）", "ok")
        elif candidates:
            ac.log(f"识别测试：{len(candidates)} 个候选都不在桌宠窗口上", "warn")
        else:
            ac.log("识别测试：当前没有发现宝箱", "info")
        return {
            "count": len(chests),
            "candidates": len(candidates),
            "ts": ac.STATE.preview_ts,
        }

    def save_settings(self, data: dict) -> dict:
        if "interval_sec" in data:
            try:
                self.cfg["interval_sec"] = max(5, min(60, int(data["interval_sec"])))
            except Exception:
                pass
        if "auto_start" in data:
            self.cfg["auto_start"] = bool(data["auto_start"])
        ac.save_config(self.cfg)
        if self.enabled.is_set():
            ac.STATE.next_scan_at = time.time() + float(self.cfg.get("interval_sec", 15))
        return {
            "interval": int(self.cfg.get("interval_sec", 15)),
            "auto_start": bool(self.cfg.get("auto_start", True)),
        }


def _make_handler(srv: UiServer):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            pass

        # ---------- helpers ----------
        def _auth(self, query: dict | None = None) -> bool:
            if self.headers.get("X-Token") == srv.token:
                return True
            if query and query.get("token", [""])[0] == srv.token:
                return True
            self._json({"error": "forbidden"}, 403)
            return False

        def _json(self, obj, code: int = 200) -> None:
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except Exception:
                pass

        def _file(self, path: Path, ctype: str) -> None:
            try:
                data = path.read_bytes()
            except Exception:
                self._json({"error": "not found"}, 404)
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            try:
                self.wfile.write(data)
            except Exception:
                pass

        def _body(self) -> dict:
            try:
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b"{}"
                data = json.loads(raw.decode("utf-8") or "{}")
                return data if isinstance(data, dict) else {}
            except Exception:
                return {}

        # ---------- routes ----------
        def do_GET(self):
            try:
                u = urlparse(self.path)
                q = parse_qs(u.query)
                if u.path in ("/", "/index.html"):
                    return self._file(_ui_dir() / "index.html", "text/html; charset=utf-8")
                if u.path == "/favicon.ico":
                    self.send_response(204)
                    self.end_headers()
                    return
                if u.path == "/preview.jpg":
                    if not self._auth(q):
                        return
                    return self._file(ac.BASE_DIR / "preview.jpg", "image/jpeg")
                if u.path == "/api/state":
                    if not self._auth():
                        return
                    return self._json(srv.state_dict())
                self._json({"error": "not found"}, 404)
            except Exception as e:
                self._json({"error": str(e)}, 500)

        def do_POST(self):
            try:
                u = urlparse(self.path)
                if not self._auth():
                    return
                data = self._body()
                if u.path == "/api/toggle":
                    srv.set_enabled(bool(data.get("enabled")))
                    return self._json({"ok": True, "enabled": srv.enabled.is_set()})
                if u.path == "/api/scan":
                    return self._json(srv.scan_now())
                if u.path == "/api/config":
                    return self._json({"ok": True, **srv.save_settings(data)})
                if u.path == "/api/clear_log":
                    with ac.STATE.lock:
                        ac.STATE.logs.clear()
                    return self._json({"ok": True})
                if u.path == "/api/open":
                    target = data.get("target")
                    path = {
                        "log": ac.LOG_PATH,
                        "folder": ac.BASE_DIR,
                        "preview": ac.BASE_DIR / "detect_preview.png",
                    }.get(target)
                    if path and Path(path).exists():
                        os.startfile(str(path))
                        return self._json({"ok": True})
                    return self._json({"ok": False, "error": "not found"}, 404)
                if u.path == "/api/quit":
                    self._json({"ok": True})
                    srv.request_quit()
                    return
                self._json({"error": "not found"}, 404)
            except Exception as e:
                self._json({"error": str(e)}, 500)

    return Handler


def run_ui(cfg: dict, auto_stop_sec: float = 0) -> None:
    import pystray
    from pystray import Menu, MenuItem

    enabled = threading.Event()
    if cfg.get("auto_start", True):
        enabled.set()
    ac.STATE.enabled = enabled.is_set()
    ac.STATE.status = "监控中" if enabled.is_set() else "已暂停"

    stop_event = threading.Event()
    monitor = threading.Thread(
        target=ac.monitor_loop,
        args=(cfg, stop_event, enabled),
        daemon=True,
    )
    monitor.start()

    srv = UiServer(cfg, stop_event, enabled)
    port = srv.start()
    url = f"http://127.0.0.1:{port}/?token={srv.token}"
    try:
        webbrowser.open(url)
    except Exception as e:
        ac.log(f"打开面板失败: {e}", "warn")
    ac.log(f"控制面板已启动（127.0.0.1:{port}）", "ok")

    def open_panel(icon_, item):
        try:
            webbrowser.open(url)
        except Exception:
            pass

    def toggle(icon_, item):
        srv.set_enabled(not enabled.is_set())
        icon_.update_menu()

    def quit_app(icon_, item):
        stop_event.set()
        srv.stop()
        icon_.stop()

    def status_text(item):
        return f"状态：{ac.STATE.status}"

    icon = pystray.Icon(
        ac.APP_NAME,
        ac.make_icon_image(),
        "WuwaTappo 自动开箱",
        menu=Menu(
            MenuItem(status_text, None, enabled=False),
            MenuItem("打开控制面板", open_panel),
            MenuItem("暂停 / 继续", toggle),
            Menu.SEPARATOR,
            MenuItem("退出", quit_app),
        ),
    )
    srv.on_quit = lambda: (stop_event.set(), icon.stop())

    if auto_stop_sec:
        threading.Timer(auto_stop_sec, icon.stop).start()
    else:
        def _hello():
            try:
                icon.notify("控制面板已在浏览器打开。", "WuwaTappo 自动开箱")
            except Exception:
                pass
        threading.Timer(1.5, _hello).start()

    try:
        icon.run()
    except BaseException:
        import traceback
        ac.log("托盘异常:\n" + traceback.format_exc(), "err")

    stop_event.set()
    srv.stop()
    monitor.join(timeout=2)
    ac.log("程序已退出", "info")
