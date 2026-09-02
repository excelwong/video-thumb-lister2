#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""目录扫描：生成缩略图并把电影结构写入 json 文档。

对外两个入口：
  * build_new(dir_abs, out_json, ...)  —— 2.1.1 扫描目录生成一份新的 json
  * append_dir(doc, dir_abs, ...)      —— 2.1.2 把一个目录追加进已有的 json

扫描在调用方线程里同步执行（GUI 会丢到后台线程），通过 progress_cb 汇报进度，
通过 should_cancel 支持中途取消。
"""

import os

import jsonstore
import thumbs


class ScanStats(object):
    """一次扫描的统计结果。"""

    def __init__(self):
        self.total = 0
        self.ok = 0            # 成功拿到缩略图的
        self.fail = 0          # 缩略图生成失败的
        self.added = 0         # 写入 json 的电影数（含无缩略图的）
        self.cancelled = False
        self.by_source = {     # 缩略图来源分布
            "cover-webp": 0,     # 番号同名 webp 直接复制（保持 webp）
            "cover-convert": 0,  # 番号同名 jpg/png/bmp 等转成 webp
            "frame": 0,          # ffmpeg 抽帧（webp）
            "cached": 0,         # 复用 *_thumb/ 已有副本
            "none": 0,           # 没有缩略图
        }
        self.failures = []     # [(video_path, reason)]

    def as_text(self):
        parts = [
            "共 %d 部电影" % self.total,
            "缩略图成功 %d" % self.ok,
        ]
        if self.fail:
            parts.append("失败 %d" % self.fail)
        src = self.by_source
        detail = []
        if src["cover-webp"]:
            detail.append("封面webp复制 %d" % src["cover-webp"])
        if src["cover-convert"]:
            detail.append("转webp %d" % src["cover-convert"])
        if src["frame"]:
            detail.append("ffmpeg抽帧 %d" % src["frame"])
        if src["cached"]:
            detail.append("复用缓存 %d" % src["cached"])
        if detail:
            parts.append("（" + "、".join(detail) + "）")
        if self.cancelled:
            parts.append("【已取消】")
        return "，".join(parts)


def _rel_to_root(abs_path, root):
    """abs_path 相对 root 的路径（用 / 分隔）；root 为 None 时返回 None。"""
    if not root:
        return None
    rel = jsonstore._rel_to(abs_path, root)
    return rel


def scan_into_doc(doc, dir_abs, thumb_dir=None, ffmpeg=None, force=False,
                  progress_cb=None, should_cancel=None):
    """扫描 dir_abs（含子目录），生成缩略图并写入 doc。

    thumb_dir 为 json 同级的缩略图缓存目录：若提供，则把每部电影的缩略图
    副本复制进去（文件名由 jsonstore.thumb_basename 决定，写入 movie["tfile"]），
    浏览界面只读这里，避免每次刷界面都去读可能在网络盘上的视频目录。
    progress_cb(done, total, message) 会被周期性调用；
    should_cancel() 返回 True 时中止扫描。
    返回 ScanStats。
    """
    dir_abs = os.path.abspath(dir_abs)
    root_idx, _rel_dir = jsonstore.ensure_root(doc, dir_abs)
    root = jsonstore.root_path(doc, root_idx)
    if thumb_dir:
        os.makedirs(thumb_dir, exist_ok=True)

    videos = thumbs.scan_videos(dir_abs)
    stats = ScanStats()
    stats.total = len(videos)

    for i, vp in enumerate(videos):
        if should_cancel and should_cancel():
            stats.cancelled = True
            break

        name = os.path.splitext(os.path.basename(vp))[0]
        rel_file = _rel_to_root(vp, root)
        if rel_file is None:
            # 兜底：取不到相对路径就存文件名
            rel_file = os.path.basename(vp)
        rel_dir = os.path.dirname(rel_file).replace("\\", "/")
        # 电影名 = 视频文件所在目录的目录名（无专属子目录时退化为视频文件名去扩展名）
        if rel_dir:
            name = rel_dir.rsplit("/", 1)[-1]
        else:
            name = os.path.splitext(os.path.basename(vp))[0]

        try:
            size = os.path.getsize(vp)
        except OSError:
            size = 0

        thumb_abs = None
        source = None
        ok = False
        rel_thumb = None
        tfile = jsonstore.thumb_basename(root_idx, rel_file)
        dst = os.path.join(thumb_dir, tfile) if thumb_dir else None
        if dst and os.path.isfile(dst) and thumbs._is_webp_file(dst):
            # 需求#3：*_thumb/ 下已有同名有效 webp -> 直接复用，跳过生成
            thumb_abs = dst
            source = "cached"
            ok = True
        elif thumb_dir:
            # 缩略图直接落在 json 同级的 *_thumb/ 子目录（dst），
            # 不再写到视频所在目录，避免污染可能在网络盘上的片库。
            try:
                thumb_abs, source, ok = thumbs.make_thumb(
                    vp, ffmpeg, force=force, out_path=dst)
            except Exception as e:  # noqa: BLE001
                stats.failures.append((vp, "%s: %s" % (type(e).__name__, e)))
            # 生成失败 / 产物不是真 webp -> 不记 tfile，避免界面读到坏文件
            if not (thumb_abs and os.path.isfile(thumb_abs)
                    and thumbs._is_webp_file(thumb_abs)):
                tfile = None
                thumb_abs = None
        else:
            # 兜底：未提供 thumb_dir 时才写回视频所在目录
            # （兼容直接调用 scan_into_doc 的场景）；此时不记 tfile
            tfile = None
            try:
                thumb_abs, source, ok = thumbs.make_thumb(vp, ffmpeg, force=force)
            except Exception as e:  # noqa: BLE001
                stats.failures.append((vp, "%s: %s" % (type(e).__name__, e)))
            rel_thumb = _rel_to_root(thumb_abs, root) if thumb_abs else None

        jsonstore.insert_movie(doc, root_idx, rel_dir, name,
                               rel_file, rel_thumb, size, tfile=tfile)
        stats.added += 1

        if ok:
            stats.ok += 1
            stats.by_source[source] = stats.by_source.get(source, 0) + 1
        else:
            stats.fail += 1
            stats.by_source["none"] = stats.by_source.get("none", 0) + 1
            if not any(f[0] == vp for f in stats.failures):
                stats.failures.append((vp, "缩略图生成失败"))

        if progress_cb:
            progress_cb(i + 1, len(videos),
                        "(%d/%d) %s" % (i + 1, len(videos), name))

    return stats


def build_new(dir_abs, out_json, ffmpeg=None, force=False,
              progress_cb=None, should_cancel=None):
    """2.1.1：扫描目录，生成一份全新的 json 文件（含缩略图缓存副本）。

    返回 (doc, stats)。调用方负责在需要时保存。
    """
    doc = jsonstore.new_doc(dir_abs)
    thumb_dir = jsonstore.thumb_dir_for(out_json)
    os.makedirs(thumb_dir, exist_ok=True)
    stats = scan_into_doc(doc, dir_abs, thumb_dir=thumb_dir, ffmpeg=ffmpeg,
                          force=force, progress_cb=progress_cb,
                          should_cancel=should_cancel)
    if not stats.cancelled:
        jsonstore.save_doc(out_json, doc)
    return doc, stats


def append_dir(doc, dir_abs, out_json, ffmpeg=None, force=False,
               progress_cb=None, should_cancel=None):
    """2.1.2：把一个目录追加进已有的 json 文档并保存（含缩略图缓存副本）。

    返回 stats。
    """
    thumb_dir = jsonstore.thumb_dir_for(out_json)
    os.makedirs(thumb_dir, exist_ok=True)
    stats = scan_into_doc(doc, dir_abs, thumb_dir=thumb_dir, ffmpeg=ffmpeg,
                          force=force, progress_cb=progress_cb,
                          should_cancel=should_cancel)
    if not stats.cancelled:
        jsonstore.save_doc(out_json, doc)
    return stats

