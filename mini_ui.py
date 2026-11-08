#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""迷你控制窗：启动 / 停止 / 详细面板。

默认不打开浏览器，点“启动监控”并确认鼠标控制提示后才开始静默监控。
"""

from __future__ import annotations

import ctypes
import sys
import threading
import time
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import messagebox

from PIL import Image, ImageTk

import autochest as ac
from ui_server import UiServer

BG = "#0a0d12"
CARD = "#121822"
CARD2 = "#151d29"
LINE = "#232c3a"
TEXT = "#e9eef7"
MUTED = "#8c99ac"
DIM = "#5f6c80"
ACCENT = "#f0b45e"
ACCENT2 = "#ffd08a"
OK = "#5dcaa5"
WARN = "#e8b45c"
ERR = "#e57373"


def _asset(name: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / name


class MiniApp:
    def __init__(self, cfg: dict, auto_stop_sec: float = 0):
        self.cfg = cfg
        # 按“鼠标所在显示器”的缩放来定尺寸：多屏混用缩放时，
        # 主屏 100% + 副屏 150% 也不会把窗口画得过大或过小。
        self.screen = ac.cursor_monitor()
        self.scale = max(1.0, self.screen.scale)
        self.stop_event = threading.Event()
        self.enabled = threading.Event()
        self._log_sig = ""
        self._scanning = False

        ac.STATE.enabled = False
        ac.STATE.started = False
        ac.STATE.status = "未启动"
        ac.STATE.next_scan_at = 0

        self.root = tk.Tk()
        self.root.title("WuwaTappo 自动开箱")
        self.root.configure(bg=BG)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        self._set_icon()
        self._build()
        self._position()
        self._dark_titlebar()

        self.monitor = threading.Thread(
            target=ac.monitor_loop,
            args=(cfg, self.stop_event, self.enabled),
            daemon=True,
        )
        self.monitor.start()

        self.srv = UiServer(cfg, self.stop_event, self.enabled)
        self.port = self.srv.start()
        self.url = f"http://127.0.0.1:{self.port}/?token={self.srv.token}"
        ac.log("已就绪，点击“启动监控”开始", "info")

        if auto_stop_sec:
            self.root.after(int(auto_stop_sec * 1000), self.root.destroy)
        self.root.after(300, self.tick)

    # ---------- window ----------

    def s(self, n: int) -> int:
        return max(1, int(n * self.scale))

    def _set_icon(self):
        try:
            icon = _asset("icon.ico")
            if icon.exists():
                self.root.iconbitmap(default=str(icon))
        except Exception:
            pass

    def _position(self):
        """放到鼠标所在显示器的左下角；不再固定用主屏尺寸/系统 DPI。

        尺寸用 minsize/maxsize 钉死，而不是 resizable(False, False)：
        后者的 WM_NORESIZE 在把窗口拖到“另一块不同缩放比例”的显示器上时，
        会被系统按 DPI 反复压缩横向尺寸（越拖越窄，直到最小宽度）。
        """
        w, h = self.s(380), self.s(540)
        mon = self.screen
        left, top, right, bottom = mon.work if mon.work != (0, 0, 0, 0) else (mon.left, mon.top, mon.right, mon.bottom)
        x = left + self.s(40)
        y = max(top + self.s(40), bottom - h - self.s(80))
        x = min(x, max(left, right - w))

        self.root.resizable(True, True)  # 交给 minsize/maxsize 限制，避免跨 DPI 拖动时被压缩
        self.root.minsize(w, h)
        self.root.maxsize(w, h)
        self.root.geometry(f"{w}x{h}+{x}+{y}")

    def _dark_titlebar(self):
        try:
            self.root.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            dwm = ctypes.windll.dwmapi
            on = ctypes.c_int(1)
            dwm.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(on), 4)
            cap = ctypes.c_int(0x0016110E)
            dwm.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(cap), 4)
            txt = ctypes.c_int(0x00F7EEE9)
            dwm.DwmSetWindowAttribute(hwnd, 36, ctypes.byref(txt), 4)
        except Exception:
            pass

    def _build(self):
        pad = self.s(16)
        gap = self.s(8)

        header = tk.Frame(self.root, bg=BG)
        header.pack(fill="x", padx=pad, pady=(self.s(14), self.s(10)))

        try:
            img = Image.open(_asset("icon.png")).resize((self.s(34), self.s(34)), Image.LANCZOS)
            self.logo_img = ImageTk.PhotoImage(img)
            logo = tk.Label(header, image=self.logo_img, bg=BG, bd=0)
        except Exception:
            logo = tk.Label(header, text="宝", bg="#2a2114", fg=ACCENT,
                            font=("Microsoft YaHei UI", 14, "bold"), width=2)
        logo.pack(side="left")

        titles = tk.Frame(header, bg=BG)
        titles.pack(side="left", padx=(self.s(10), 0))
        tk.Label(titles, text="自动开箱", bg=BG, fg=TEXT,
                 font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w")
        tk.Label(titles, text="WuwaTappo 桌宠助手", bg=BG, fg=DIM,
                 font=("Microsoft YaHei UI", 8)).pack(anchor="w")

        self.status_label = tk.Label(header, text="● 未启动", bg=BG, fg=DIM,
                                     font=("Microsoft YaHei UI", 9))
        self.status_label.pack(side="right")

        hero = tk.Frame(self.root, bg=CARD, highlightthickness=1,
                        highlightbackground=LINE)
        hero.pack(fill="x", padx=pad)
        tk.Frame(hero, bg=ACCENT, height=1).pack(fill="x")

        body = tk.Frame(hero, bg=CARD)
        body.pack(fill="x", padx=self.s(14), pady=(self.s(10), self.s(12)))

        top = tk.Frame(body, bg=CARD)
        top.pack(fill="x")
        tk.Label(top, text="本次已领取", bg=CARD, fg=MUTED,
                 font=("Microsoft YaHei UI", 9)).pack(side="left")
        self.total_label = tk.Label(top, text="累计 0", bg=CARD, fg=ACCENT,
                                    font=("Microsoft YaHei UI", 9))
        self.total_label.pack(side="right")

        mid = tk.Frame(body, bg=CARD)
        mid.pack(fill="x", pady=(self.s(2), 0))
        self.claimed_label = tk.Label(mid, text="0", bg=CARD, fg=TEXT,
                                      font=("Microsoft YaHei UI", 30, "bold"))
        self.claimed_label.pack(side="left")
        tk.Label(mid, text="个", bg=CARD, fg=MUTED,
                 font=("Microsoft YaHei UI", 9)).pack(side="left", padx=(self.s(4), 0), pady=(self.s(12), 0))

        right = tk.Frame(mid, bg=CARD)
        right.pack(side="right")
        self.countdown_label = tk.Label(right, text="--", bg=CARD, fg=TEXT,
                                        font=("Microsoft YaHei UI", 15, "bold"))
        self.countdown_label.pack(anchor="e")
        tk.Label(right, text="秒后检查", bg=CARD, fg=DIM,
                 font=("Microsoft YaHei UI", 8)).pack(anchor="e")

        self.hint_label = tk.Label(body, text="点击下方按钮开始静默监控", bg=CARD, fg=MUTED,
                                   font=("Microsoft YaHei UI", 9), anchor="w")
        self.hint_label.pack(fill="x", pady=(self.s(6), 0))

        self.toggle_btn = tk.Button(
            self.root, text="启动监控", command=self.toggle,
            bg=ACCENT, fg="#241706", activebackground=ACCENT2, activeforeground="#241706",
            relief="flat", bd=0, cursor="hand2",
            font=("Microsoft YaHei UI", 12, "bold"), height=2,
        )
        self.toggle_btn.pack(fill="x", padx=pad, pady=(self.s(12), self.s(8)))

        row = tk.Frame(self.root, bg=BG)
        row.pack(fill="x", padx=pad)
        self.panel_btn = tk.Button(
            row, text="打开详细面板", command=self.open_panel,
            bg=CARD2, fg=TEXT, activebackground="#1d2735", activeforeground=TEXT,
            relief="flat", bd=0, cursor="hand2", highlightthickness=1,
            highlightbackground=LINE, font=("Microsoft YaHei UI", 9), height=1,
        )
        self.panel_btn.pack(side="left", fill="x", expand=True)
        self.scan_btn = tk.Button(
            row, text="立即识别", command=self.scan_now,
            bg=CARD2, fg=TEXT, activebackground="#1d2735", activeforeground=TEXT,
            relief="flat", bd=0, cursor="hand2", highlightthickness=1,
            highlightbackground=LINE, font=("Microsoft YaHei UI", 9), height=1,
        )
        self.scan_btn.pack(side="left", fill="x", expand=True, padx=(self.s(8), 0))

        log_card = tk.Frame(self.root, bg=CARD, highlightthickness=1,
                            highlightbackground=LINE)
        log_card.pack(fill="both", expand=True, padx=pad, pady=(self.s(10), self.s(8)))
        log_head = tk.Frame(log_card, bg=CARD)
        log_head.pack(fill="x", padx=self.s(12), pady=(self.s(7), self.s(4)))
        tk.Label(log_head, text="运行日志", bg=CARD, fg=MUTED,
                 font=("Microsoft YaHei UI", 8)).pack(side="left")

        self.log_text = tk.Text(
            log_card, height=6, bg="#0d121a", fg="#a9b6c8", bd=0, relief="flat",
            font=("Consolas", 8), wrap="word", state="disabled",
            padx=self.s(8), pady=self.s(4), insertwidth=0,
        )
        self.log_text.pack(fill="both", expand=True, padx=self.s(6), pady=(0, self.s(6)))
        self.log_text.tag_configure("t", foreground="#526079")
        self.log_text.tag_configure("ok", foreground="#7fd6b6")
        self.log_text.tag_configure("warn", foreground=WARN)
        self.log_text.tag_configure("err", foreground=ERR)

        tk.Label(self.root, text="仅模拟点击 · 不改存档 · v0.3", bg=BG, fg=DIM,
                 font=("Microsoft YaHei UI", 8)).pack(pady=(0, self.s(8)))

    # ---------- actions ----------

    def _ask(self, title: str, text: str, default_no: bool = True) -> bool:
        flags = 0x00000004 | 0x00000030 | 0x00040000  # YESNO | WARNING | TOPMOST
        if default_no:
            flags |= 0x00000100  # DEFBUTTON2
        try:
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            return ctypes.windll.user32.MessageBoxW(hwnd, text, title, flags) == 6
        except Exception:
            return messagebox.askyesno(title, text, parent=self.root, icon="warning", default="no")

    def toggle(self):
        if self.enabled.is_set():
            self.enabled.clear()
            ac.STATE.enabled = False
            ac.STATE.status = "已暂停"
            ac.STATE.next_scan_at = 0
            ac.log("已暂停监控", "warn")
            self.refresh()
            return

        self.root.deiconify()
        self.root.lift()
        ok = self._ask(
            "启动前提示",
            "启动后会静默监控宝箱。\n\n"
            "发现宝箱时，软件会短暂移动并点击鼠标（约 0.3 秒）。\n"
            "请不要在游戏关键操作、拖拽文件或远程控制时运行。\n\n"
            "是否启动？",
        )
        if not ok:
            return

        self.enabled.set()
        ac.STATE.enabled = True
        ac.STATE.started = True
        ac.STATE.status = "监控中"
        ac.STATE.next_scan_at = time.time() + float(self.cfg.get("interval_sec", 15))
        ac.log("已启动监控", "ok")
        self.refresh()

    def open_panel(self):
        try:
            webbrowser.open(self.url)
            ac.log("已打开详细面板", "info")
        except Exception as e:
            ac.log(f"打开详细面板失败: {e}", "warn")

    def scan_now(self):
        if self._scanning:
            return
        self._scanning = True
        self.scan_btn.config(text="识别中…", state="disabled")

        def work():
            try:
                self.srv.scan_now()
            except Exception as e:
                ac.log(f"识别失败: {e}", "err")
            finally:
                self.root.after(0, self._scan_done)

        threading.Thread(target=work, daemon=True).start()

    def _scan_done(self):
        self._scanning = False
        self.scan_btn.config(text="立即识别", state="normal")
        self.refresh()

    def on_close(self):
        if self.enabled.is_set():
            ok = self._ask(
                "退出确认",
                "正在监控中，退出后将停止自动领取。\n\n确定退出？",
            )
            if not ok:
                return
        self.stop_event.set()
        try:
            self.srv.stop()
        except Exception:
            pass
        ac.log("程序已退出", "info")
        self.root.destroy()

    # ---------- refresh ----------

    def refresh(self):
        enabled = self.enabled.is_set()
        status = ac.STATE.status or ("监控中" if enabled else "未启动")

        if enabled:
            color = OK
        elif ac.STATE.started:
            color = WARN
        else:
            color = DIM
        self.status_label.config(text=f"● {status}", fg=color)

        self.claimed_label.config(text=str(ac.STATE.claimed))
        self.total_label.config(text=f"累计 {ac.STATE.total}")

        if enabled:
            remain = max(0, int(ac.STATE.next_scan_at - time.time()))
            self.countdown_label.config(text=str(remain))
            if status == "领取中":
                self.hint_label.config(text="正在领取宝箱…")
            elif status == "检查中":
                self.hint_label.config(text="正在检查桌面…")
            else:
                self.hint_label.config(text="静默监控中，发现宝箱会自动点击")
        else:
            self.countdown_label.config(text="--")
            self.hint_label.config(text="点击下方按钮开始静默监控")

        if enabled:
            self.toggle_btn.config(text="停止监控", bg="#232c3a", fg="#dce6f3",
                                   activebackground="#2b3648", activeforeground="#ffffff")
        else:
            self.toggle_btn.config(text="启动监控", bg=ACCENT, fg="#241706",
                                   activebackground=ACCENT2, activeforeground="#241706")

        self.refresh_log()

    def refresh_log(self):
        with ac.STATE.lock:
            logs = list(ac.STATE.logs)[-6:]
        sig = "|".join(f"{l['t']}{l['msg']}{l['level']}" for l in logs)
        if sig == self._log_sig:
            return
        self._log_sig = sig
        self.log_text.config(state="normal")
        self.log_text.delete("1.0", "end")
        for l in logs:
            self.log_text.insert("end", f"{l['t']} ", "t")
            self.log_text.insert("end", l["msg"] + "\n", l["level"])
        self.log_text.see("end")
        self.log_text.config(state="disabled")

    def tick(self):
        self.refresh()
        self.root.after(500, self.tick)

    def run(self):
        self.root.mainloop()


def run_mini(cfg: dict, auto_stop_sec: float = 0) -> None:
    app = MiniApp(cfg, auto_stop_sec=auto_stop_sec)
    app.run()
