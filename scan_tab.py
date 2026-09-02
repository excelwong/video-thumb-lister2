#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tab ①：扫描 / 生成 JSON。

2.1.1 扫描某个目录（含子目录）→ 生成一份 json（目录树 + 电影 + 缩略图），
      扫描过程中为每个视频生成缩略图（同名 jpg 复制 / 同名 webp 转 jpg / ffmpeg 抽帧）。
2.1.2 把某个目录追加进一份已存在的 json。

扫描在后台线程执行，通过队列 + after 轮询把进度安全地送回主线程
（绝不在工作线程里碰 Tk，否则 Python 3.13 下会触发 GIL 致命崩溃）。
"""

import os
import queue
import threading
import traceback
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import jsonstore
import scanner
import thumbs

_MAX_LOG_LINES = 2000


class ScanTab(object):
    def __init__(self, parent, app):
        self.app = app
        self.frame = ttk.Frame(parent)
        self.q = queue.Queue()
        self._poll_scheduled = False
        self.busy = False
        self.cancel_event = threading.Event()
        self.last_json_path = None
        self._build()
        self._poll()

    # ---------------------------------------------------------------- UI

    def _build(self):
        f = self.frame
        f.columnconfigure(1, weight=1)

        # --- 模式 ---
        top = ttk.LabelFrame(f, text="扫描模式", padding=8)
        top.grid(row=0, column=0, columnspan=3, sticky="ew", padx=8, pady=(8, 4))
        self.mode_var = tk.StringVar(value="new")
        ttk.Radiobutton(top, text="① 新建：扫描目录，生成一份新的 JSON",
                        value="new", variable=self.mode_var,
                        command=self._on_mode).grid(row=0, column=0, sticky="w")
        ttk.Radiobutton(top, text="② 追加：把目录添加到已存在的 JSON",
                        value="append", variable=self.mode_var,
                        command=self._on_mode).grid(row=0, column=1,
                                                    sticky="w", padx=(24, 0))

        # --- 路径 ---
        mid = ttk.LabelFrame(f, text="路径", padding=8)
        mid.grid(row=1, column=0, columnspan=3, sticky="ew", padx=8, pady=4)
        mid.columnconfigure(1, weight=1)

        ttk.Label(mid, text="扫描目录：").grid(row=0, column=0, sticky="w")
        self.dir_var = tk.StringVar()
        ttk.Entry(mid, textvariable=self.dir_var).grid(
            row=0, column=1, sticky="ew", padx=4)
        ttk.Button(mid, text="浏览…", command=self._pick_dir).grid(
            row=0, column=2, padx=(4, 0))

        ttk.Label(mid, text="JSON 文件：").grid(row=1, column=0, sticky="w",
                                               pady=(6, 0))
        self.json_var = tk.StringVar()
        ttk.Entry(mid, textvariable=self.json_var).grid(
            row=1, column=1, sticky="ew", padx=4, pady=(6, 0))
        self.json_btn = ttk.Button(mid, text="另存为…",
                                   command=self._pick_json)
        self.json_btn.grid(row=1, column=2, padx=(4, 0), pady=(6, 0))

        # --- 选项 ---
        opt = ttk.Frame(f, padding=(8, 2))
        opt.grid(row=2, column=0, columnspan=3, sticky="ew")
        self.force_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(opt, text="强制重新生成缩略图（忽略已有缓存）",
                        variable=self.force_var).pack(side="left")
        ttk.Label(opt, text="　缩略图统一存 webp：番号同名 webp 直接复制，"
                            "jpg/png/bmp 等转成 webp（原文件不变），"
                            "都没有则 ffmpeg 抽第 10 秒帧（不足 10 秒取首帧）",
                  foreground="#666").pack(side="left")

        # --- 操作按钮 ---
        bar = ttk.Frame(f, padding=(8, 6))
        bar.grid(row=3, column=0, columnspan=3, sticky="ew")
        self.start_btn = ttk.Button(bar, text="开始扫描",
                                    command=self.on_start)
        self.start_btn.pack(side="left")
        self.cancel_btn = ttk.Button(bar, text="取消", state="disabled",
                                     command=self.on_cancel)
        self.cancel_btn.pack(side="left", padx=6)
        self.open_btn = ttk.Button(bar, text="打开 JSON 所在目录",
                                   state="disabled", command=self._open_json_dir)
        self.open_btn.pack(side="left")

        # --- 进度 ---
        self.prog_var = tk.StringVar(value="就绪")
        ttk.Label(f, textvariable=self.prog_var).grid(
            row=4, column=0, columnspan=3, sticky="w", padx=10)
        self.progress = ttk.Progressbar(f, mode="determinate")
        self.progress.grid(row=5, column=0, columnspan=3, sticky="ew",
                           padx=10, pady=(0, 6))

        # --- 日志 ---
        logf = ttk.LabelFrame(f, text="日志", padding=4)
        logf.grid(row=6, column=0, columnspan=3, sticky="nsew", padx=8,
                  pady=(0, 8))
        f.rowconfigure(6, weight=1)
        logf.rowconfigure(0, weight=1)
        logf.columnconfigure(0, weight=1)
        self.log = tk.Text(logf, height=10, wrap="word", state="disabled")
        sb = ttk.Scrollbar(logf, command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set)
        self.log.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")

        # 首次打开给 ffmpeg 状态一个提示
        ff = thumbs.find_ffmpeg()
        if ff:
            self._log("ffmpeg: %s" % ff)
        else:
            self._log("⚠ 未找到 ffmpeg：只能用同名 jpg 封面，webp 无法转换，"
                      "也无法从视频抽帧。")

    # ------------------------------------------------------------ 事件

    def _on_mode(self):
        if self.mode_var.get() == "new":
            self.json_btn.configure(text="另存为…")
        else:
            self.json_btn.configure(text="选择…")

    def _pick_dir(self):
        d = filedialog.askdirectory(title="选择要扫描的目录")
        if d:
            self.dir_var.set(d)
            # 新建模式下，按扫描目录自动推导默认 json 路径：
            # <项目>/output/gallery_{序号}_{末级目录名}.json
            if self.mode_var.get() == "new" and not self.json_var.get().strip():
                self.json_var.set(jsonstore.gallery_json_path(d))

    def _pick_json(self):
        if self.mode_var.get() == "new":
            p = filedialog.asksaveasfilename(
                title="保存扫描结果为", defaultextension=".json",
                filetypes=[("JSON", "*.json"), ("所有文件", "*.*")],
                initialfile="movie_index.json")
        else:
            p = filedialog.askopenfilename(
                title="选择已存在的 JSON",
                filetypes=[("JSON", "*.json"), ("所有文件", "*.*")])
        if p:
            self.json_var.set(p)

    def _open_json_dir(self):
        p = self.last_json_path
        if p and os.path.isfile(p):
            try:
                os.startfile(os.path.dirname(os.path.abspath(p)))
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------ 扫描

    def on_start(self):
        if self.busy:
            return
        scan_dir = self.dir_var.get().strip()
        json_path = self.json_var.get().strip()
        mode = self.mode_var.get()

        if not scan_dir or not os.path.isdir(scan_dir):
            messagebox.showerror("提示", "请先选择一个有效的扫描目录。")
            return
        if not json_path:
            messagebox.showerror("提示", "请指定 JSON 文件路径。")
            return
        if mode == "append" and not os.path.isfile(json_path):
            messagebox.showerror("提示", "追加模式下 JSON 文件必须已存在。\n"
                                        "若要新建，请切换到模式①。")
            return

        self.busy = True
        self.cancel_event.clear()
        self._set_controls(False)
        self.progress["value"] = 0
        self.prog_var.set("正在扫描…")
        self._log("=" * 60)
        self._log("开始扫描：%s" % scan_dir)
        self._log("模式：%s　JSON：%s"
                  % ("新建" if mode == "new" else "追加", json_path))

        threading.Thread(
            target=self._worker,
            args=(mode, scan_dir, json_path, self.force_var.get()),
            daemon=True).start()

    def on_cancel(self):
        if self.busy:
            self.cancel_event.set()
            self.prog_var.set("正在取消…（等当前这个视频处理完就停）")

    def _set_controls(self, enabled):
        st = "normal" if enabled else "disabled"
        self.start_btn.configure(state=st)
        self.cancel_btn.configure(state="disabled" if enabled else "normal")
        self.open_btn.configure(state="normal" if enabled else "disabled")
        for w in (self.json_btn,):
            w.configure(state=st)

    def _worker(self, mode, scan_dir, json_path, force):
        try:
            ffmpeg = thumbs.find_ffmpeg()
            if not ffmpeg:
                self.q.put(("log", "⚠ 未找到 ffmpeg，缩略图只能依赖已有的同名 jpg 封面。"))

            def cb(done, total, msg):
                self.q.put(("progress", done, total, msg))

            if mode == "new":
                _doc, stats = scanner.build_new(
                    scan_dir, json_path, ffmpeg=ffmpeg, force=force,
                    progress_cb=cb, should_cancel=self.cancel_event.is_set)
            else:
                doc, err = jsonstore.load_doc(json_path)
                if err:
                    self.q.put(("error", "读取 JSON 失败：" + err))
                    return
                stats = scanner.append_dir(
                    doc, scan_dir, json_path, ffmpeg=ffmpeg, force=force,
                    progress_cb=cb, should_cancel=self.cancel_event.is_set)

            for vp, reason in stats.failures[:20]:
                self.q.put(("log", "　失败：%s（%s）"
                            % (os.path.basename(vp), reason)))
            if len(stats.failures) > 20:
                self.q.put(("log", "　… 另有 %d 个失败" % (len(stats.failures) - 20)))
            self.q.put(("done", not stats.cancelled, stats.as_text(), json_path))
        except Exception:  # noqa: BLE001
            self.q.put(("error", traceback.format_exc()))

    # --------------------------------------------------- 队列轮询（主线程）

    def _poll(self):
        self._poll_scheduled = False
        try:
            while True:
                item = self.q.get_nowait()
                self._handle(item)
        except queue.Empty:
            pass
        self._schedule_poll()

    def _schedule_poll(self):
        if self._poll_scheduled:
            return
        self._poll_scheduled = True
        self.frame.after(80, self._poll)

    def _handle(self, item):
        kind = item[0]
        if kind == "progress":
            _done, total, msg = item[1], item[2], item[3]
            pct = 0 if not total else int(item[1] * 100.0 / total)
            self.progress["value"] = pct
            self.prog_var.set("扫描中 %d/%d　%s" % (item[1], total, msg))
        elif kind == "log":
            self._log(item[1])
        elif kind == "error":
            self._log("【错误】\n" + item[1])
            messagebox.showerror("扫描出错", item[1][:1500])
            self._finish(False)
        elif kind == "done":
            ok, text, path = item[1], item[2], item[3]
            self._log("完成：" + text)
            self.prog_var.set(("已完成 → " if ok else "已取消 → ") + text
                              + "　（切到「② 浏览 / 播放」查看）")
            self.last_json_path = path
            self._finish(True)
            if ok:
                self.app.set_status("扫描完成：" + text)

    def _finish(self, ok):
        self.busy = False
        self._set_controls(True)
        if ok:
            self.progress["value"] = 100

    # ------------------------------------------------------------ 工具

    def _log(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        # 限制行数，避免长时间扫描把内存撑爆
        lines = int(self.log.index("end-1c").split(".")[0])
        if lines > _MAX_LOG_LINES:
            self.log.delete("1.0", "%d.0" % (lines - _MAX_LOG_LINES + 1))
        self.log.see("end")
        self.log.configure(state="disabled")
