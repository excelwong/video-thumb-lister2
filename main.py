#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Video Thumb Lister 2.0 —— GUI 主入口。

两个独立的 Tab 页：
  ① 扫描 / 生成 JSON   —— 扫描目录生成缩略图与 json；或把目录追加进已有 json
  ② 浏览 / 播放        —— 左侧目录树 + 右侧缩略图网格，左键播放、右键菜单

启动后自动最大化。双击 启动.bat 即可运行。
"""

import os
import sys
import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

# 保证从任意目录启动都能 import 同目录下的模块
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import browse_tab  # noqa: E402
import scan_tab  # noqa: E402

APP_TITLE = "Video Thumb Lister 2.0"


def _setup_fonts(root):
    """中文 Windows 上把默认字体换成雅黑，避免默认点阵字体发虚。"""
    fam = "Microsoft YaHei UI"
    try:
        if fam.lower() not in [f.lower() for f in tkfont.families()]:
            fam = "Microsoft YaHei"
            if fam.lower() not in [f.lower() for f in tkfont.families()]:
                return
    except Exception:  # noqa: BLE001
        return
    for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
        try:
            f = tkfont.nametofont(name)
            f.configure(family=fam, size=10)
        except Exception:  # noqa: BLE001
            pass


class App(object):
    def __init__(self, root):
        self.root = root
        root.title(APP_TITLE)
        # 先设一个合理尺寸，再最大化：某些 Tk 版本在窗口尚未 realize 时
        # 直接 state("zoomed") 会被忽略（video-ad-cutter 里踩过这个坑），
        # 所以延后到主循环起来之后再设一次。
        root.geometry("1440x900")
        root.minsize(1000, 650)
        root.after(0, self._zoom_window)

        # F11 切换真正的全屏，Esc 退出
        self._fullscreen = False
        root.bind("<F11>", self._toggle_fullscreen)
        root.bind("<Escape>", self._exit_fullscreen)

        nb = ttk.Notebook(root)
        nb.pack(fill="both", expand=True, padx=6, pady=6)

        self.scan = scan_tab.ScanTab(nb, self)
        self.browse = browse_tab.BrowseTab(nb, self)
        nb.add(self.scan.frame, text="  ① 扫描 / 生成 JSON  ")
        nb.add(self.browse.frame, text="  ② 浏览 / 播放  ")
        nb.bind("<<NotebookTabChanged>>", self._on_tab_changed)
        self.nb = nb

        # 状态栏（两个 Tab 共用）
        self.status_var = tk.StringVar(value="就绪")
        bar = ttk.Frame(root)
        bar.pack(fill="x", side="bottom")
        ttk.Label(bar, textvariable=self.status_var, anchor="w",
                  relief="sunken", padding=(6, 3)).pack(fill="x", side="left",
                                                        expand=True)
        ttk.Label(bar, text="F11 全屏 / Esc 退出", anchor="e",
                  relief="sunken", padding=(6, 3)).pack(side="right")

    # ---------- 窗口 ----------

    def _zoom_window(self):
        try:
            self.root.state("zoomed")
        except Exception:  # noqa: BLE001
            try:
                self.root.attributes("-zoomed", True)
            except Exception:  # noqa: BLE001
                pass

    def _toggle_fullscreen(self, _event=None):
        self._fullscreen = not self._fullscreen
        try:
            self.root.attributes("-fullscreen", self._fullscreen)
        except Exception:  # noqa: BLE001
            pass
        return "break"

    def _exit_fullscreen(self, _event=None):
        if self._fullscreen:
            self._fullscreen = False
            try:
                self.root.attributes("-fullscreen", False)
            except Exception:  # noqa: BLE001
                pass
        return "break"

    # ---------- Tab 联动 ----------

    def _on_tab_changed(self, _event=None):
        """切到浏览页时，若扫描页刚产出/更新过 json，就自动载入。"""
        try:
            idx = self.nb.index(self.nb.select())
        except Exception:  # noqa: BLE001
            return
        if idx == 1:
            path = getattr(self.scan, "last_json_path", None)
            if path and os.path.isfile(path):
                cur = getattr(self.browse, "json_path", None)
                if cur != path:
                    self.browse.load_json(path)

    def set_status(self, text):
        self.status_var.set(text)


def main():
    root = tk.Tk()
    _setup_fonts(root)
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
