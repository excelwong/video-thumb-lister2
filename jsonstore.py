#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""扫描结果 JSON 的数据结构与读写。

JSON 结构
---------
{
  "settings": {
    "version": "2.0",
    "root":  "D:/Movies",          // 首次扫描目录的**绝对路径**
    "roots": ["D:/Movies"],        // 所有已扫描根目录（追加扫描会新增）
    "created_at": "...",
    "updated_at": "...",
    "video_count": 123
  },
  "tree": {                        // 递归目录树，内部**全部相对路径**
    "dirs": {                      // 子目录 -> 同名结构
      "动作片": {"dirs": {}, "files": []}
    },
    "files": [                     // 直接位于该层的电影
      {
        "name":  "ABC-123",                    // 电影名（= 所在目录名）
        "file":  "动作片/ABC-123.mp4",          // 相对所属 root 的路径
        "thumb": null,                         // 源缩略图相对路径（新方案不写视频目录，恒为 null）
        "tfile": "0__动作片__ABC-123.mp4.webp", // *_thumb/ 缓存副本文件名
        "size":  1234567,                      // 字节
        "root":  0                             // 属于 settings.roots[0]
      }
    ]
  }
}

要点：
  * 绝对路径只出现在 settings 里（root / roots），目录树内一律相对路径，
    这样整个 json 换台机器、换个盘符也能带着走，只要改 settings 即可。
  * 追加扫描一个不在原 root 下的目录时，会往 roots 里新增一条，
    并给该目录的电影记上对应的 root 索引，仍然保持相对路径。
  * 缩略图只存一份，且只在 json 同级目录：
      - 缓存副本：json 同级的 <json 名>_thumb/ 目录里的 webp（**界面只读这里**，
        避免每次刷界面都去读可能在网络盘上的视频目录）。
        webp 封面直接复制、其它格式转 webp、无封面则 ffmpeg 抽帧（webp）。
    movie["tfile"] 记录副本的文件名（如 `0__动作__ABC-123.mp4.webp`），
    movie["thumb"] 恒为 null（新方案不把缩略图写到视频目录，避免污染片库）。
"""

import datetime
import hashlib
import json
import os
import re

VERSION = "2.0"

# 树节点的键
DIRS = "dirs"
FILES = "files"

# 输出目录：<项目根>/output/，所有 json 与缩略图缓存都落在这里
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(PROJECT_DIR, "output")

# 文件名里不能出现的字符（Windows + POSIX 取交集）
_ILLEGAL = re.compile(r'[\\/:*?"<>|\r\n\t]')


def _now():
    return datetime.datetime.now().isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# 创建 / 读写
# --------------------------------------------------------------------------

def new_doc(root_abs):
    """新建一个空文档，root_abs 为首次扫描目录的绝对路径。"""
    root_abs = os.path.abspath(root_abs)
    return {
        "settings": {
            "version": VERSION,
            "root": root_abs,
            "roots": [root_abs],
            "created_at": _now(),
            "updated_at": _now(),
            "video_count": 0,
        },
        "tree": {DIRS: {}, FILES: []},
    }


def load_doc(path):
    """读取 json 文件，返回 (doc, error)。error 非 None 表示失败。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            doc = json.load(f)
    except Exception as e:  # noqa: BLE001
        return None, "%s: %s" % (type(e).__name__, e)
    if not isinstance(doc, dict) or "tree" not in doc:
        return None, "不是有效的扫描结果 json（缺少 tree 字段）"
    doc.setdefault("settings", {})
    st = doc["settings"]
    st.setdefault("version", VERSION)
    st.setdefault("roots", [st["root"]] if st.get("root") else [])
    if not st.get("root") and st["roots"]:
        st["root"] = st["roots"][0]
    doc["tree"].setdefault(DIRS, {})
    doc["tree"].setdefault(FILES, [])
    return doc, None


def save_doc(path, doc):
    """写回 json 文件（更新统计与时间戳）。"""
    doc["settings"]["updated_at"] = _now()
    doc["settings"]["video_count"] = count_movies(doc)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return path


# --------------------------------------------------------------------------
# 根目录管理（支持追加其它盘/目录）
# --------------------------------------------------------------------------

def ensure_root(doc, dir_abs):
    """把一个目录归入某个 root，返回 (root_index, rel_dir)。

    - 若 dir_abs 位于已有 root 之内（含就是 root 本身），复用该 root；
    - 否则把 dir_abs 作为新 root 追加到 settings.roots。
    rel_dir 是 dir_abs 相对该 root 的相对路径（"" 表示就是 root 自身）。
    """
    roots = doc["settings"].setdefault("roots", [])
    dir_abs = os.path.abspath(dir_abs)
    best_i, best_rel = None, None
    for i, r in enumerate(roots):
        rel = _rel_to(dir_abs, r)
        if rel is not None:
            # 选最“贴合”的那个 root（相对路径最短）
            if best_rel is None or len(rel) < len(best_rel):
                best_i, best_rel = i, rel
    if best_i is not None:
        return best_i, best_rel
    roots.append(dir_abs)
    if not doc["settings"].get("root"):
        doc["settings"]["root"] = dir_abs
    return len(roots) - 1, ""


def _rel_to(path, root):
    """path 在 root 之内则返回相对路径（用 / 分隔，"" 表示相同）；否则 None。

    ⚠️ 不能用 os.path.normcase 来算：它在 Windows 上会把整条路径转成小写，
    导致 json 里存成 "动作/abc-123.mp4" 这种丢失原始大小写的路径。
    这里用 os.path.relpath，它只做字符串比较，能原样保留大小写。
    """
    try:
        pa, ra = os.path.abspath(path), os.path.abspath(root)
        rel = os.path.relpath(pa, ra)
    except (ValueError, OSError):
        # 不同盘符等无法求相对路径的情况
        return None
    if rel == os.curdir:
        return ""
    if rel == os.pardir or rel.startswith(os.pardir + os.sep):
        return None
    return rel.replace(os.sep, "/")


def root_path(doc, idx):
    """取第 idx 个 root 的绝对路径；越界返回 None。"""
    roots = doc["settings"].get("roots") or []
    try:
        return roots[idx]
    except (IndexError, TypeError):
        return None


# --------------------------------------------------------------------------
# 插入 / 删除
# --------------------------------------------------------------------------

def _node_for_rel(doc, rel_dir, create=True):
    """按相对目录路径定位/创建树节点。rel_dir 用 / 分隔，"" 表示根层。"""
    node = doc["tree"]
    if not rel_dir:
        return node
    for part in rel_dir.split("/"):
        if not part:
            continue
        d = node.setdefault(DIRS, {})
        if create:
            node = d.setdefault(part, {DIRS: {}, FILES: []})
        else:
            nxt = d.get(part)
            if nxt is None:
                return None
            node = nxt
    return node


def insert_movie(doc, root_idx, rel_dir, name, rel_file, rel_thumb, size,
                 tfile=None):
    """往目录树里插入一部电影。返回插入的 movie 字典。

    rel_file / rel_thumb 为**相对所属 root** 的路径（用 / 分隔）。
    tfile 为缩略图在 json 同级 <json名>_thumb/ 缓存子目录里的文件名
    （相对于该子目录）；无缓存副本时为 None。
    若同路径已存在，则覆盖更新。
    """
    node = _node_for_rel(doc, rel_dir)
    node.setdefault(FILES, [])
    movie = {
        "name": name,
        "file": rel_file,
        "thumb": rel_thumb,
        "size": size,
    }
    if tfile:
        movie["tfile"] = tfile
    if root_idx:
        movie["root"] = root_idx
    # 去重：同相对路径已存在则替换
    for i, m in enumerate(node[FILES]):
        if m.get("file") == rel_file:
            node[FILES][i] = movie
            return movie
    node[FILES].append(movie)
    return movie


def remove_movie(doc, root_idx, rel_file):
    """从 json 中剔除一部电影（**只改 json，不动磁盘文件**）。

    返回 True 表示确实删掉了。删除后若所在目录变空，会连带清理空的目录节点。
    """
    rel_file = rel_file.replace("\\", "/")
    parts = rel_file.split("/")
    file_name = parts[-1]
    dir_parts = parts[:-1]

    path = []          # 记录从根到目标目录的节点链，便于回溯清理空目录
    node = doc["tree"]
    for p in dir_parts:
        d = node.get(DIRS) or {}
        nxt = d.get(p)
        if nxt is None:
            return False
        path.append((node, p))
        node = nxt

    files = node.get(FILES) or []
    hit = None
    for i, m in enumerate(files):
        if m.get("file") == rel_file and (m.get("root") or 0) == root_idx:
            hit = i
            break
    if hit is None:
        # 兼容：文件名匹配也算命中
        for i, m in enumerate(files):
            if (m.get("file") or "").split("/")[-1] == file_name \
                    and (m.get("root") or 0) == root_idx:
                hit = i
                break
    if hit is None:
        return False
    files.pop(hit)

    # 回溯清理空目录（目录里没电影、也没子目录就删掉）
    while path:
        parent, name = path.pop()
        cur = (parent.get(DIRS) or {}).get(name)
        if cur is None:
            break
        if not (cur.get(FILES) or []) and not (cur.get(DIRS) or {}):
            del parent[DIRS][name]
        else:
            break
    return True


# --------------------------------------------------------------------------
# 遍历 / 统计
# --------------------------------------------------------------------------

def iter_nodes(doc, node=None, rel_prefix=""):
    """深度优先遍历目录树，产出 (rel_dir, node)。rel_dir 用 / 分隔。"""
    if node is None:
        node = doc["tree"]
    yield rel_prefix, node
    for name in sorted((node.get(DIRS) or {}).keys()):
        child_rel = (rel_prefix + "/" + name) if rel_prefix else name
        yield from iter_nodes(doc, node[DIRS][name], child_rel)


def iter_movies(doc, node=None, rel_prefix=""):
    """遍历所有电影，产出 (rel_dir, movie)。"""
    for rel_dir, nd in iter_nodes(doc, node, rel_prefix):
        for m in (nd.get(FILES) or []):
            yield rel_dir, m


def count_movies(doc):
    return sum(1 for _ in iter_movies(doc))


def count_in_subtree(node):
    """统计某个节点（含所有子目录）下的电影数量。"""
    total = len(node.get(FILES) or [])
    for child in (node.get(DIRS) or {}).values():
        total += count_in_subtree(child)
    return total


def movie_abs_path(doc, movie):
    """把 movie 的相对路径还原为绝对路径（视频文件）。"""
    idx = movie.get("root") or 0
    root = root_path(doc, idx)
    if not root:
        return None
    rel = (movie.get("file") or "").replace("/", os.sep)
    return os.path.join(root, rel) if rel else root


def movie_abs_thumb(doc, movie):
    """把 movie 的缩略图相对路径还原为绝对路径。"""
    idx = movie.get("root") or 0
    root = root_path(doc, idx)
    thumb = movie.get("thumb")
    if not root or not thumb:
        return None
    return os.path.join(root, thumb.replace("/", os.sep))


def movie_name(movie):
    """电影的展示名：视频文件所在目录的目录名。

    符合「一部电影一个文件夹」的整理习惯——电影名取自包含该视频的文件夹名，
    而非视频文件名本身（视频文件名常带 -C/-UC 等版本后缀，与文件夹名不同）。
    回退策略：目录名为空（视频直接放在根目录下、没有专属子目录）时，
    退回 movie["name"]，再退回视频文件名（去扩展名）。
    """
    f = movie.get("file") or ""
    d = os.path.dirname(f.replace("\\", "/"))
    if d:
        return d.rsplit("/", 1)[-1]
    return (movie.get("name") or os.path.splitext(os.path.basename(f))[0]) or ""


# --------------------------------------------------------------------------
# 缩略图缓存副本（json 同级的 <json名>_thumb/ 目录）
# --------------------------------------------------------------------------

def gallery_json_path(scan_dir):
    """根据「扫描目录」推导默认的 json 输出路径。

    落在 OUTPUT_DIR 下，文件名带**盘符**：``gallery_{盘符}_{末级目录名}.json``。
    例：扫描 ``X:/中文字幕`` -> ``<output>/gallery_X_中文字幕.json``；
        扫描 ``D:/片库/中文字幕`` -> ``<output>/gallery_D_中文字幕.json``。
    盘符取大写、去冒号；无盘符（网络盘/相对路径）则退化为 ``gallery``。
    若同一盘符+目录名已存在（重新扫描同名目录），追加序号 ``_2`` / ``_3`` … 去重。
    末级目录名里的非法文件名字符会被替换成下划线。
    """
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    last = os.path.basename(os.path.normpath(scan_dir))
    if not last:
        # 扫描的是盘符根目录（如 D:/）时退回盘符
        last = (os.path.splitdrive(scan_dir)[0] or "gallery").strip(":/\\")
    last = _ILLEGAL.sub("_", last).strip("._ ").strip()
    if not last:
        last = "gallery"
    drive = (os.path.splitdrive(scan_dir)[0] or "").strip(":/\\")
    drive = drive.upper() if drive else "gallery"
    # 扫描盘符根目录（如 X:/）时 last 也会退化成盘符，避免 gallery_X_X 这种冗余
    if last == drive:
        base = "gallery_%s" % drive
    else:
        base = "gallery_%s_%s" % (drive, last)
    n = 1
    while True:
        name = base + ".json" if n == 1 else "%s_%d.json" % (base, n)
        if not os.path.exists(os.path.join(OUTPUT_DIR, name)):
            break
        n += 1
    return os.path.join(OUTPUT_DIR, name)


def thumb_dir_for(json_path):
    """json 同级的缩略图缓存目录：``<json 名(去扩展名)>_thumb``。"""
    return os.path.splitext(json_path)[0] + "_thumb"


def thumb_basename(root_idx, rel_file):
    """某部电影在 thumb 子目录里的文件名（相对该子目录）。

    用 root 索引 + 相对路径做唯一标识；目录分隔符 / 换成 __，
    其它文件系统非法字符换成 _，确保 Windows 上文件名合法且唯一。
    """
    safe = _ILLEGAL.sub("_", rel_file)
    return ("%d__%s" % (root_idx, safe)).replace("/", "__") + ".webp"


def thumb_abs(json_path, movie):
    """某部电影的缩略图缓存副本的绝对路径；无 tfile 时返回 None。"""
    tfile = movie.get("tfile")
    if not tfile:
        return None
    return os.path.join(thumb_dir_for(json_path), tfile)


def remove_directory(doc, dir_rel):
    """从 json 中删除整个目录子树（含所有子目录与电影）。

    返回被删除的 movie 列表（调用方据此清理 thumb 子目录里的缩略图）。
    **只改 json，不动磁盘**。删空后不清理其父目录是否为空（由调用方决定）。
    dir_rel 为空（根层）时拒绝删除，返回 []。
    """
    if not dir_rel:
        return []
    node = _node_for_rel(doc, dir_rel, create=False)
    if node is None:
        return []
    removed = [m for _d, m in iter_movies(doc, node, dir_rel)]
    if "/" in dir_rel:
        parent_rel, name = dir_rel.rsplit("/", 1)
    else:
        parent_rel, name = "", dir_rel
    pnode = _node_for_rel(doc, parent_rel, create=False)
    if pnode is not None:
        (pnode.get(DIRS) or {}).pop(name, None)
        # 顺手清理因本次删除而变空的父链
        cur = parent_rel
        while cur:
            pr, pn = (cur.rsplit("/", 1) if "/" in cur else ("", cur))
            p2 = _node_for_rel(doc, pr, create=False)
            if p2 is None:
                break
            c = (p2.get(DIRS) or {}).get(pn)
            if c is None:
                break
            if (c.get(FILES) or []) or (c.get(DIRS) or {}):
                break
            (p2.get(DIRS) or {}).pop(pn, None)
            cur = pr
    return removed

