#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tab ②：浏览 / 播放。

读取一份扫描结果 json：
  * 左侧 —— 整个目录树（ttk.Treeview）
  * 右侧 —— 电影缩略图网格（列数随窗口宽度自适应）；每张卡片下方显示
    电影名称、视频文件名、视频文件大小（左键播放，右键菜单）
  * 左键点击缩略图 —— 调起本机 VLC / PotPlayer 播放
  * 右键菜单 —— 「打开所在目录」/「在 JSON 中剔除」
      （剔除只是从 json 里删掉这部电影的节点，不动磁盘上的任何文件，
        删除后自动刷新目录树与网格）

缩略图显示走「PIL 解码 → PNG bytes → tk.PhotoImage(data=…)」，
绕开本机「应用程序控制策略」会拦截的 Pillow ImageTk DLL。
"""

import base64
import io
import os
import subprocess
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import jsonstore
import player

# 缩略图在网格里的显示宽度
THUMB_W = 200
CARD_W = THUMB_W + 24
# 每批渲染多少张卡片（分批渲染，避免大目录卡住界面）
BATCH = 24
# PhotoImage 缓存上限。缩略图统一预存在 json 同级的 *_thumb/ 子目录，
# 单张约 200KB；本机 64G 内存，按 5000 张 ≈ 1GB 估算完全够用，故放宽到 5000。
CACHE_MAX = 5000


class BrowseTab(object):
    def __init__(self, parent, app):
        self.app = app
        self.frame = ttk.Frame(parent)
        self.json_path = None
        self.doc = None
        self._node_rel = {}        # tree iid -> rel_dir
        self._photo_cache = {}     # 缩略图绝对路径 -> PhotoImage
        self._cache_order = []
        self._render_queue = []
        self._render_idx = 0
        self._rendering = False
        self._render_token = 0
        self._build()

    # ---------------------------------------------------------------- UI

    def _build(self):
        f = self.frame
        f.columnconfigure(0, weight=1)
        f.rowconfigure(1, weight=1)

        # --- 顶部工具条 ---
        top = ttk.Frame(f, padding=(8, 6))
        top.grid(row=0, column=0, sticky="ew")
        ttk.Button(top, text="载入 JSON…", command=self._pick_json).pack(
            side="left")
        self.path_var = tk.StringVar(value="（尚未载入 JSON）")
        ttk.Label(top, textvariable=self.path_var,
                  foreground="#444").pack(side="left", padx=8)

        ttk.Label(top, text="　播放器：").pack(side="left")
        self.player_var = tk.StringVar(value="auto")
        self.player_box = ttk.Combobox(
            top, textvariable=self.player_var, width=14, state="readonly",
            values=["auto"])
        self.player_box.pack(side="left")
        self.player_box.set("auto")
        self._refresh_player_list()

        ttk.Label(top, text="　搜索：").pack(side="left", padx=(12, 0))
        self.search_var = tk.StringVar()
        ent = ttk.Entry(top, textvariable=self.search_var, width=18)
        ent.pack(side="left")
        ent.bind("<Return>", lambda e: self._render_current())
        ttk.Button(top, text="清空",
                   command=self._clear_search).pack(side="left", padx=4)

        # --- 主体：左树 + 右网格（分隔条可拖拽，调整左右宽度）---
        body = ttk.PanedWindow(f, orient="horizontal")
        body.grid(row=1, column=0, sticky="nsew", padx=8, pady=(0, 8))

        left = ttk.Frame(body)
        left.rowconfigure(0, weight=1)
        left.columnconfigure(0, weight=1)
        self.tree = ttk.Treeview(left, show="tree", selectmode="browse")
        # 树列随窗格变宽自动伸展，目录名过长也能看全（另有横向滚动条兜底）
        self.tree.column("#0", width=260, stretch=True)
        tsb = ttk.Scrollbar(left, command=self.tree.yview)
        self.tree.configure(yscrollcommand=tsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        tsb.grid(row=0, column=1, sticky="ns")
        # 横向滚动条：窗格不够宽时仍可拖到看全文件名
        self.treexsb = ttk.Scrollbar(left, orient="horizontal",
                                     command=self.tree.xview)
        self.tree.configure(xscrollcommand=self.treexsb.set)
        self.treexsb.grid(row=1, column=0, sticky="ew")
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)
        self._bind_tree_menu()
        body.add(left, weight=0)

        right = ttk.Frame(body)
        right.rowconfigure(0, weight=1)
        right.columnconfigure(0, weight=1)
        self.canvas = tk.Canvas(right, highlightthickness=0)
        vsb = ttk.Scrollbar(right, command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=vsb.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        self.inner = ttk.Frame(self.canvas)
        self._win = self.canvas.create_window((0, 0), window=self.inner,
                                              anchor="nw")
        self.inner.bind("<Configure>", self._on_inner_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self._bind_mousewheel()
        body.add(right, weight=1)

        self.hint_var = tk.StringVar(value="请先载入一份扫描结果 JSON。")
        ttk.Label(f, textvariable=self.hint_var, foreground="#666",
                  padding=(10, 0, 0, 6)).grid(row=2, column=0, sticky="w")

    def _clear_search(self):
        self.search_var.set("")
        self._render_current()

    def _refresh_player_list(self):
        vals = ["auto"]
        self._players = {}
        for p in player.list_players():
            vals.append(p["name"])
            self._players[p["name"]] = p["path"]
        vals.append("自定义…")
        self.player_box.configure(values=vals)
        if self.player_var.get() not in vals:
            self.player_var.set("auto")

    # ------------------------------------------------------------ 载入

    def _pick_json(self):
        p = filedialog.askopenfilename(
            title="选择扫描结果 JSON",
            filetypes=[("JSON", "*.json"), ("所有文件", "*.*")])
        if p:
            self.load_json(p)

    def load_json(self, path):
        doc, err = jsonstore.load_doc(path)
        if err:
            messagebox.showerror("载入失败", err)
            return False
        self.json_path = path
        self.doc = doc
        self._photo_cache.clear()
        self._cache_order = []
        self.path_var.set(path)
        self._rebuild_tree()
        return True

    # ------------------------------------------------------- 目录树

    def _rebuild_tree(self):
        self.tree.delete(*self.tree.get_children())
        self._node_rel = {}
        if not self.doc:
            return
        st = self.doc["settings"]
        root_abs = st.get("root") or (st.get("roots") or [""])[0]
        root_name = os.path.basename(str(root_abs).rstrip("\\/")) or root_abs
        total = jsonstore.count_movies(self.doc)
        iid = self.tree.insert("", "end", text="%s  (%d)" % (root_name, total),
                               open=True)
        self._node_rel[iid] = ""
        self._add_tree_children(iid, self.doc["tree"])
        self.tree.selection_set(iid)
        self._render_current()

    def _add_tree_children(self, parent_iid, node):
        for name in sorted((node.get("dirs") or {}).keys()):
            child = node["dirs"][name]
            cnt = jsonstore.count_in_subtree(child)
            rel = self._node_rel.get(parent_iid, "")
            child_rel = (rel + "/" + name) if rel else name
            iid = self.tree.insert(parent_iid, "end",
                                   text="%s  (%d)" % (name, cnt))
            self._node_rel[iid] = child_rel
            self._add_tree_children(iid, child)

    def _on_tree_select(self, _event=None):
        self._render_current()

    def _selected_rel(self):
        sel = self.tree.selection()
        if not sel:
            return ""
        return self._node_rel.get(sel[0], "")

    # ------------------------------------------------------- 网格渲染

    def _current_movies(self):
        """当前选中目录（含所有子目录）下的电影，按搜索词过滤。"""
        if not self.doc:
            return []
        rel = self._selected_rel()
        # 定位到该 rel 对应的节点
        node = self.doc["tree"]
        if rel:
            for part in rel.split("/"):
                node = (node.get("dirs") or {}).get(part)
                if node is None:
                    return []
        kw = self.search_var.get().strip().lower()
        out = []
        for _d, m in jsonstore.iter_movies(self.doc, node, rel):
            if kw and kw not in jsonstore.movie_name(m).lower():
                continue
            out.append(m)
        return out

    def _render_current(self):
        movies = self._current_movies()
        self._render_token += 1
        token = self._render_token
        self._rendering = False
        for w in self.inner.winfo_children():
            w.destroy()
        self._render_queue = movies
        self._render_idx = 0
        self._rendering = True

        n = len(movies)
        rel = self._selected_rel() or "（根目录）"
        self.hint_var.set("%s　共 %d 部电影　（左键播放，右键菜单）"
                          % (rel, n))
        if n == 0:
            self._rendering = False
            ttk.Label(self.inner, text="（该目录下没有电影）",
                      foreground="#888", padding=20).grid(row=0, column=0)
            return
        self._render_batch(token)

    def _render_batch(self, token):
        if not self._rendering or token != self._render_token:
            return
        cols = self._compute_cols()
        for _ in range(BATCH):
            if self._render_idx >= len(self._render_queue):
                self._rendering = False
                break
            m = self._render_queue[self._render_idx]
            self._add_card(m, self._render_idx, cols)
            self._render_idx += 1
        self.inner.update_idletasks()
        self._on_inner_configure()
        if self._rendering:
            self.frame.after(10, lambda: self._render_batch(token))

    def _compute_cols(self):
        w = self.canvas.winfo_width()
        if not w or w < 50:
            w = self.frame.winfo_width() or 1000
        return max(1, max(240, w - 20) // CARD_W)

    def _add_card(self, movie, idx, cols):
        r, c = divmod(idx, cols)
        card = ttk.Frame(self.inner, padding=4, relief="ridge")
        card.grid(row=r, column=c, padx=6, pady=6, sticky="n")
        for i in range(cols):
            self.inner.columnconfigure(i, weight=1)

        photo = self._load_thumb(movie)
        lbl_img = ttk.Label(card)
        if photo is not None:
            lbl_img.configure(image=photo)
            lbl_img.image = photo       # 防止被 GC
        else:
            lbl_img.configure(text="（无缩略图）", width=24,
                              anchor="center", foreground="#999")
            lbl_img.configure(relief="groove", padding=(0, 40))
        lbl_img.pack()

        name = jsonstore.movie_name(movie)
        show = name if len(name) <= 26 else name[:25] + "…"
        lbl_name = ttk.Label(card, text=show, wraplength=THUMB_W,
                             justify="center", font=("TkDefaultFont", 10, "bold"))
        lbl_name.pack(pady=(4, 0))

        # 视频文件名（取相对路径里的文件名）
        fname = os.path.basename(movie.get("file") or "")
        fshow = fname if len(fname) <= 34 else fname[:33] + "…"
        lbl_file = ttk.Label(card, text=fshow, wraplength=THUMB_W,
                             justify="center", foreground="#555",
                             font=("TkDefaultFont", 9))
        lbl_file.pack(pady=(2, 0))

        # 视频文件大小
        sz = self._fmt_size(movie.get("size") or 0)
        lbl_size = ttk.Label(card, text=sz, justify="center",
                             foreground="#888", font=("TkDefaultFont", 9))
        lbl_size.pack(pady=(1, 0))

        for w in (card, lbl_img, lbl_name, lbl_file, lbl_size):
            w.bind("<Button-1>", lambda e, m=movie: self._on_play(m))
            w.bind("<Button-3>", lambda e, m=movie: self._show_menu(e, m))
            try:
                w.configure(cursor="hand2")
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def _fmt_size(size):
        """把字节数格式化成易读的大小（B / KB / MB / GB / TB）。"""
        try:
            size = float(size)
        except (TypeError, ValueError):
            return ""
        if size <= 0:
            return ""
        units = ["B", "KB", "MB", "GB", "TB"]
        for u in units:
            if size < 1024 or u == units[-1]:
                if u == "B":
                    return "%d %s" % (size, u)
                return "%.1f %s" % (size, u)
            size /= 1024.0
        return "%.1f TB" % size

    def _load_thumb(self, movie):
        """把缩略图解码成 tk.PhotoImage（带缓存）。

        优先读 json 同级的 ``*_thumb/`` 缓存副本（扫描时已生成，
        避免每次刷界面去读可能在网络盘上的视频目录）；
        没有缓存副本（旧 json / 缺图）时回退到视频同目录的源缩略图。
        走 PIL → PNG bytes → PhotoImage(data=…)，绕开 ImageTk DLL。
        """
        path = self._thumb_path(movie)
        if not path or not os.path.isfile(path):
            # 兼容旧 json：回退到视频同目录的源缩略图
            src = jsonstore.movie_abs_thumb(self.doc, movie)
            if src and os.path.isfile(src):
                path = src
        if not path:
            return None
        key = os.path.normcase(path)
        if key in self._photo_cache:
            return self._photo_cache[key]
        try:
            from PIL import Image
            with Image.open(path) as im:
                im = im.convert("RGB")
                w, h = im.size
                if w <= 0 or h <= 0:
                    return None
                th = max(1, int(h * THUMB_W / w))
                im = im.resize((THUMB_W, th))
                buf = io.BytesIO()
                im.save(buf, format="PNG")
            photo = tk.PhotoImage(data=base64.b64encode(buf.getvalue()))
        except Exception:  # noqa: BLE001
            return None
        # 简易 LRU
        self._photo_cache[key] = photo
        self._cache_order.append(key)
        while len(self._cache_order) > CACHE_MAX:
            old = self._cache_order.pop(0)
            self._photo_cache.pop(old, None)
        return photo

    def _thumb_path(self, movie):
        """该电影在 *_thumb/ 缓存子目录里的缩略图绝对路径（无则 None）。"""
        if not self.json_path:
            return None
        return jsonstore.thumb_abs(self.json_path, movie)

    def _delete_thumb_file(self, movie):
        """从 *_thumb/ 缓存子目录里删掉这部电影对应的缩略图（不动磁盘源图）。"""
        if not self.json_path:
            return
        p = jsonstore.thumb_abs(self.json_path, movie)
        if p and os.path.isfile(p):
            try:
                os.remove(p)
            except OSError:
                pass

    # ------------------------------------------------------- 滚动

    def _on_inner_configure(self, _event=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas_configure(self, event=None):
        self.canvas.itemconfigure(self._win, width=self.canvas.winfo_width())
        self._on_inner_configure()
        # 窗口宽度变化时重排（节流：停止拖动后再排）
        if self._resize_job:
            try:
                self.frame.after_cancel(self._resize_job)
            except Exception:  # noqa: BLE001
                pass
        self._resize_job = self.frame.after(250, self._rerender_keep_pos)

    _resize_job = None

    def _rerender_keep_pos(self):
        self._resize_job = None
        if self.doc and not self._rendering:
            self._render_current()

    def _bind_mousewheel(self):
        def on_mousewheel(event):
            self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        self.canvas.bind_all("<MouseWheel>", on_mousewheel)

    # ------------------------------------------------------- 播放

    def _player_path(self):
        choice = self.player_var.get()
        if choice == "自定义…":
            p = filedialog.askopenfilename(
                title="选择播放器程序",
                filetypes=[("可执行程序", "*.exe"), ("所有文件", "*.*")])
            if p:
                self._players[p] = p
                vals = list(self.player_box.cget("values"))
                vals.insert(len(vals) - 1, p)
                self.player_box.configure(values=vals)
                self.player_var.set(p)
            return p or None
        if choice == "auto":
            return None
        return self._players.get(choice)

    def _on_play(self, movie):
        path = jsonstore.movie_abs_path(self.doc, movie)
        if not path:
            messagebox.showerror("提示", "无法定位该电影的路径。")
            return
        if not os.path.isfile(path):
            messagebox.showerror(
                "提示", "文件不存在（可能已被移动或删除）：\n%s" % path)
            return
        ok, msg = player.play(path, self._player_path())
        if ok:
            self.app.set_status("正在用 %s 播放：%s"
                                % (msg, jsonstore.movie_name(movie)))
        else:
            messagebox.showerror("播放失败", msg)

    # ------------------------------------------------------- 右键菜单

    def _show_menu(self, event, movie):
        menu = tk.Menu(self.frame, tearoff=0)
        menu.add_command(label="播放（%s）" % (jsonstore.movie_name(movie) or ""),
                         command=lambda: self._on_play(movie))
        menu.add_separator()
        menu.add_command(label="打开所在目录",
                         command=lambda: self._open_dir(movie))
        menu.add_command(label="在 JSON 中剔除",
                         command=lambda: self._remove_movie(movie))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _open_dir(self, movie):
        path = jsonstore.movie_abs_path(self.doc, movie)
        if not path:
            return
        d = os.path.dirname(path)
        if not os.path.isdir(d):
            messagebox.showerror("提示", "目录不存在：\n%s" % d)
            return
        # 用资源管理器打开并选中该文件（最实用）
        try:
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        except Exception:  # noqa: BLE001
            try:
                os.startfile(d)
            except Exception:  # noqa: BLE001
                messagebox.showerror("提示", "无法打开目录：\n%s" % d)

    def _remove_movie(self, movie):
        """从 JSON 中剔除这部电影（不动磁盘文件），然后刷新。

        剔除会改动 JSON 索引，所以必须先弹确认框，把「剔除什么 / 不改什么」
        讲清楚，并默认把焦点放在「取消」上（default=NO），避免右键误点。
        """
        if not self.doc or not self.json_path:
            return
        name = jsonstore.movie_name(movie)
        rel_file = movie.get("file") or ""
        abs_path = jsonstore.movie_abs_path(self.doc, movie) or rel_file

        msg = (
            "确定要把这部电影从索引中剔除吗？\n"
            "\n"
            "　电影：%s\n"
            "　路径：%s\n"
            "\n"
            "剔除后会发生的：\n"
            "　· 从 %s 中删除该电影的节点及其缩略图信息；\n"
            "　· 所在目录若因此变空，会连带清理空的目录节点；\n"
            "　· 列表与目录树自动刷新。\n"
            "\n"
            "不会发生的（重要）：\n"
            "　· 不会删除磁盘上的视频文件；\n"
            "　· 不会删除磁盘上的封面 / 缩略图图片。\n"
            "　（重新扫描该目录即可把它加回来）"
            % (name, abs_path, os.path.basename(self.json_path))
        )
        if not messagebox.askyesno("确认剔除", msg, default=messagebox.NO,
                                   icon="warning", parent=self.frame):
            self.app.set_status("已取消剔除。")
            return
        idx = movie.get("root") or 0
        ok = jsonstore.remove_movie(self.doc, idx, rel_file)
        if not ok:
            messagebox.showwarning("提示", "没能在 JSON 中找到这部电影的节点。")
            return
        # 同步从 *_thumb/ 缓存子目录删掉对应的缩略图（不动磁盘源图）
        self._delete_thumb_file(movie)
        try:
            jsonstore.save_doc(self.json_path, self.doc)
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("保存失败", str(e))
            return
        self.app.set_status("已剔除：%s（仅从 JSON 移除）" % name)
        # 剔除后自动刷新目录树与网格
        self._rebuild_tree()

    # ------------------------------------------------------- 右键剔除目录

    def _bind_tree_menu(self):
        self.tree.bind("<Button-3>", self._on_tree_rightclick)

    def _on_tree_rightclick(self, event):
        iid = self.tree.identify_row(event.y)
        if not iid:
            return
        rel = self._node_rel.get(iid)
        if rel is None or rel == "":
            # 根节点不允许整树删除
            return
        menu = tk.Menu(self.frame, tearoff=0)
        menu.add_command(label="剔除该目录（含子目录）",
                         command=lambda: self._remove_directory(rel))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _remove_directory(self, dir_rel):
        """从 JSON 中剔除整个目录子树（含子目录与电影），并同步清理 *_thumb/。

        不动磁盘上的任何视频/封面文件；删除后自动刷新。
        """
        if not self.doc or not self.json_path:
            return
        node = jsonstore._node_for_rel(self.doc, dir_rel, create=False)
        if node is None:
            return
        cnt = jsonstore.count_in_subtree(node)
        name = dir_rel.rsplit("/", 1)[-1]
        msg = (
            "确定要剔除整个目录吗？\n"
            "\n"
            "　目录：%s\n"
            "　包含：%d 部电影（含所有子目录）\n"
            "\n"
            "剔除后会发生的：\n"
            "　· 从 %s 中删除该目录及其下所有电影节点；\n"
            "　· 同步删除 *_thumb/ 缓存子目录里这些电影的缩略图；\n"
            "　· 列表与目录树自动刷新。\n"
            "\n"
            "不会发生的（重要）：\n"
            "　· 不会删除磁盘上的视频文件；\n"
            "　· 不会删除磁盘上的封面 / 缩略图图片。"
            % (dir_rel, cnt, os.path.basename(self.json_path))
        )
        if not messagebox.askyesno("确认剔除目录", msg, default=messagebox.NO,
                                   icon="warning", parent=self.frame):
            self.app.set_status("已取消剔除目录。")
            return
        removed = jsonstore.remove_directory(self.doc, dir_rel)
        for m in removed:
            self._delete_thumb_file(m)
        try:
            jsonstore.save_doc(self.json_path, self.doc)
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("保存失败", str(e))
            return
        self.app.set_status("已剔除目录：%s（%d 部电影，仅从 JSON 移除）"
                            % (dir_rel, cnt))
        self._rebuild_tree()
