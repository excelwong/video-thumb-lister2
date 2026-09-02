#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""缩略图生成模块（复刻 video-thumb-lister 的做法）。

缩略图统一存成 **webp**（体积更小、浏览器/应用原生友好），来源优先级：
  1) 视频所在目录存在「番号同名」封面：
     - 封面本身是 **webp** -> 直接复制为缩略图（不转码，原文件保持不动）；
     - 封面是 jpg / png / bmp / gif 等其它格式 -> 用 PIL 转成 **webp**
       保存为缩略图，原文件保持不动（只读取，不改写、不改名、不删除）；
  2) 都没有 -> 用 ffmpeg 从视频约第 10 秒处抽一帧（片头常为黑场，故不取 0 秒）；
     若视频时长不足 10 秒，则直接抽**首帧**（-ss 0）。

缩略图默认生成在**视频所在目录**（``<视频全名>.thumb.webp``，下次扫描可复用）；
但 scanner 会传入 json 同级的 ``*_thumb/`` 目标路径（out_path），
使缩略图只落在本地 json 目录、不污染可能在网络盘上的片库，浏览界面只读这一份。

只依赖 Python 标准库 + 一份 ffmpeg。
"""

import os
import re
import shutil
import subprocess
import sys

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------

# 支持的视频扩展名（小写）
VIDEO_EXTS = {
    ".mp4", ".mkv", ".avi", ".mov", ".flv", ".wmv", ".m4v",
    ".ts", ".webm", ".mpg", ".mpeg", ".3gp", ".m2ts", ".vob",
    ".ogv", ".mts", ".m2v", ".mp4v", ".f4v", ".rmvb", ".rm",
}

# 同名封面图候选扩展名（按优先级，先找到的用作缩略图）
# 例：视频 a.mp4 对应封面 a.webp / a.jpg / a.png ...
COVER_EXTS = [".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"]

# ffmpeg 候选路径（按优先级查找）
FFMPEG_CANDIDATES = [
    os.environ.get("FFMPEG_BIN"),
    r"C:/Users/excel/WorkBuddy/Claw/video-ad-cutter/ffmpeg/bin/ffmpeg.exe",
    "ffmpeg",                       # 在 PATH 中
    r"C:/ffmpeg/bin/ffmpeg.exe",
    r"C:/Program Files/ffmpeg/bin/ffmpeg.exe",
    r"D:/ffmpeg/bin/ffmpeg.exe",
]

# 缩略图抽取时的目标宽度（像素）
THUMB_WIDTH = 320

# 视频名相对「核心番号」可能带的版本后缀（不区分大小写）：
#   -U / -C / -UC / -WC （去码 / 中文字幕 等标记，带连字符）
#   末尾单独的 R          （流出/修正版等标记，直接附着，无连字符）
# 例：核心番号 ABC-123 的封面 ABC-123.jpg，可用于
#   ABC-123.mp4 / ABC-123R.mp4 / ABC-123-U.mp4 / ABC-123-C.mp4 / ABC-123-UC.mp4
#   / ABC-123-WC.mp4
# ⚠️ 正则里 UC|WC 必须排在单个 U|C 之前，否则交替匹配会先命中 U 而漏掉 UC。
_COVER_TAIL_TAG = re.compile(r"-(?:UC|WC|U|C)$", re.IGNORECASE)
_COVER_TAIL_R = re.compile(r"R$", re.IGNORECASE)


# --------------------------------------------------------------------------
# ffmpeg 定位
# --------------------------------------------------------------------------

def find_ffmpeg(explicit=None):
    """返回可用的 ffmpeg 可执行文件路径；找不到返回 None。"""
    if explicit:
        return explicit if os.path.isfile(explicit) else None
    for c in FFMPEG_CANDIDATES:
        if c and os.path.isfile(c):
            return c
    which = shutil.which("ffmpeg")
    return which


def _run(cmd, timeout=120, capture_stderr=False, check=True):
    """运行子进程。Windows 下加 CREATE_NO_WINDOW，避免抽帧时黑窗狂闪。

    ⚠️ check 必须默认为 True：ffmpeg 抽帧失败时（例如 -ss 定位超出视频长度）
    会以非 0 退出码结束，若此处不检查返回码，上层会误以为成功，
    结果留下一个「抽帧成功」但根本没有 jpg 的假象。
    """
    kwargs = dict(stdout=subprocess.DEVNULL, timeout=timeout, check=check)
    kwargs["stderr"] = subprocess.PIPE if capture_stderr else subprocess.DEVNULL
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run(cmd, **kwargs)


# --------------------------------------------------------------------------
# 封面（番号同名图片）匹配
# --------------------------------------------------------------------------

def _cover_core_candidates(base):
    """由视频名（不含扩展名）推导出可能的「封面核心名」候选列表。

    先返回原名（精确匹配优先），再依次剥离末尾的 -U/-C/-UC/-WC 和 R，
    使 ABC-123R / ABC-123-U / ABC-123-UC / ABC-123-WC 等都能回退到核心名 ABC-123。
    """
    cands = [base]
    b = base
    m = _COVER_TAIL_TAG.search(b)
    if m and b[:m.start()]:
        b = b[:m.start()]
        cands.append(b)
    m = _COVER_TAIL_R.search(b)
    if m and b[:m.start()]:
        cands.append(b[:m.start()])
    seen, out = set(), []
    for c in cands:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def find_cover(video_path):
    """在视频同目录找可作封面的同名/核心名图片。

    匹配规则（按优先级）：
      1) 与视频完全同名的封面（a.mp4 -> a.webp / a.jpg …），最优先；
      2) 剥离版本后缀（-U/-C/-UC/-WC、末尾 R）后的核心名封面
         （ABC-123-U.mp4 / ABC-123-WC.mp4 -> ABC-123.webp / ABC-123.jpg …）。
    对每个候选核心名再按 COVER_EXTS 顺序查找，返回首个命中的完整路径。
    """
    d = os.path.dirname(video_path)
    base = os.path.splitext(os.path.basename(video_path))[0]
    for core in _cover_core_candidates(base):
        for ext in COVER_EXTS:
            cand = os.path.join(d, core + ext)
            if os.path.isfile(cand):
                return cand
    return None


def _is_jpeg_file(path):
    """按文件头魔数判断是否为真正的 JPEG（而非仅以 .jpg 结尾的伪文件）。"""
    try:
        with open(path, "rb") as f:
            head = f.read(3)
    except OSError:
        return False
    return head[:3] == b"\xff\xd8\xff"


def _is_webp_file(path):
    """按文件头魔数判断是否为真正的 WebP（RIFF....WEBP）。"""
    try:
        with open(path, "rb") as f:
            head = f.read(12)
    except OSError:
        return False
    return head[:4] == b"RIFF" and head[8:12] == b"WEBP"


def _is_valid_thumb(path):
    """缩略图产物是否真实有效（webp 或 jpg 均可）。"""
    return _is_webp_file(path) or _is_jpeg_file(path)


# --------------------------------------------------------------------------
# 缩略图路径与缓存
# --------------------------------------------------------------------------

def thumb_path_for_video(video_path):
    """缩略图就放在【视频所在目录】，文件名 <视频全名>.thumb.webp。

    例：C:/Videos/clip.mp4 -> C:/Videos/clip.mp4.thumb.webp
    （仅作兜底：scanner 实际会把缩略图写到 json 同级的 *_thumb/ 目录）
    """
    return video_path + ".thumb.webp"


def _need_regen(video_path, thumb_file):
    """判断缩略图是否需要重新生成。

    缩略图来源优先级：同目录同名封面图 > ffmpeg 抽帧。
    因此缓存是否失效以「实际来源」为准：
      - 缩略图不存在，或不是真正的 JPEG（旧版把 webp 改名留下的伪文件）-> 需要
      - 存在同名封面图：以封面 mtime 为准（封面被替换 -> 需要）
      - 否则以视频 mtime 为准（视频被替换/编辑 -> 需要）
    """
    if not os.path.isfile(thumb_file):
        return True
    try:
        t_thumb = os.path.getmtime(thumb_file)
    except OSError:
        return True
    if not _is_valid_thumb(thumb_file):
        return True
    ref = find_cover(video_path) or video_path
    try:
        return t_thumb < os.path.getmtime(ref)
    except OSError:
        return True


# --------------------------------------------------------------------------
# 生成缩略图
# --------------------------------------------------------------------------

def _convert_image_to_webp(src, dst):
    """把图片 src 另存为 webp 到 dst（**src 保持原样**）。

    用于 jpg/png/bmp/gif 等非 webp 封面：用 PIL 转成 webp 作为缩略图，
    而不是把 src 改名，因此原封面文件内容与文件名均不变。
    返回 True 表示 dst 是真正的 webp。
    """
    try:
        from PIL import Image
        with Image.open(src) as im:
            im.convert("RGB").save(dst, "WEBP", quality=90)
    except Exception:  # noqa: BLE001
        return False
    return _is_webp_file(dst)


def _ffprobe_path(ffmpeg):
    """由 ffmpeg 路径推导同目录的 ffprobe；找不到返回 None。"""
    if not ffmpeg:
        return None
    cand = os.path.join(os.path.dirname(ffmpeg), "ffprobe.exe")
    if os.path.isfile(cand):
        return cand
    return shutil.which("ffprobe")


def _video_duration(ffmpeg, video_path):
    """用 ffprobe 取视频时长（秒，浮点）；失败/无 ffprobe 返回 None。"""
    probe = _ffprobe_path(ffmpeg)
    if not probe:
        return None
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    try:
        out = subprocess.run(
            [probe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", video_path],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=60, check=True, creationflags=flags).stdout.decode().strip()
        return float(out)
    except Exception:  # noqa: BLE001
        return None


def _extract_frame(ffmpeg, video_path, out_webp, timeout=120):
    """用 ffmpeg 抽取「第 10 秒」的那一帧作为缩略图（输出 webp）。

    很多视频片头是黑场/淡入/标题卡，取 0 秒会黑屏，故默认约 10 秒处。
    -ss 放在 -i 之后（输出定位）可保证取到真正约 10 秒的帧，不会被关键帧
    对齐带回 0 秒。
    若视频**时长不足 10 秒**，则直接抽首帧（-ss 0）；
    抽 10 秒失败时也回退到首帧（-ss 0，兼容各类异常情况）。
    """
    # 先删掉可能存在的旧文件，否则会把上一次的残留误判成这次成功
    try:
        if os.path.isfile(out_webp):
            os.remove(out_webp)
    except OSError:
        pass

    dur = _video_duration(ffmpeg, video_path)
    if dur is not None and dur < 10:
        tries = ("0",)            # 时长 < 10 秒 -> 首帧
    else:
        tries = ("10", "0")       # 默认 10 秒，失败回退首帧

    def _try(ss):
        _run([ffmpeg, "-y", "-i", video_path, "-ss", ss, "-frames:v", "1",
              "-an", "-vf", "scale=%d:-1" % THUMB_WIDTH,
              "-c:v", "webp", "-quality", "90", out_webp],
             timeout=timeout, capture_stderr=True)
        # 命令返回 0 不代表真的出了图：短视频 -ss 超出时长时 ffmpeg 可能
        # 什么都不写却照样退出，所以必须校验产物。
        return os.path.isfile(out_webp) and _is_webp_file(out_webp)

    for ss in tries:
        try:
            if _try(ss):
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def make_thumb(video_path, ffmpeg=None, force=False, out_path=None):
    """为单个视频生成缩略图。

    out_path: 缩略图最终写入路径；为 None 时退化为「视频同目录的
    <视频全名>.thumb.webp」（thumb_path_for_video）。调用方（scanner）会传入
    json 同级的 ``*_thumb/`` 下的目标路径，使缩略图只落在 json 目录、
    不污染视频所在目录（片库常在网络盘上）。产物统一为 webp。

    返回 (thumb_path, source, ok)
      - thumb_path: 缩略图绝对路径（失败时为 None）
      - source    : "cover-webp" 直接复制同名 webp / "cover-convert" 由 jpg 等转 webp
                    / "frame" ffmpeg 抽帧（webp） / "cached" 复用已有缓存 / None 失败
      - ok        : 是否成功
    """
    thumb = out_path or thumb_path_for_video(video_path)
    if not force and not _need_regen(video_path, thumb):
        return thumb, "cached", True

    cover = find_cover(video_path)
    if cover:
        if _is_webp_file(cover):
            # 番号同名 webp 封面：直接复制为缩略图（保持 webp，不转码）
            try:
                shutil.copy2(cover, thumb)
                return thumb, "cover-webp", True
            except Exception:  # noqa: BLE001
                pass
        else:
            # jpg/png/bmp/gif 等其它封面 -> 用 PIL 转成 webp，原文件保持不动
            if _convert_image_to_webp(cover, thumb):
                return thumb, "cover-convert", True

    # 没有可用封面，或封面处理失败 -> ffmpeg 抽帧（输出 webp）
    if not ffmpeg:
        return None, None, False
    if _extract_frame(ffmpeg, video_path, thumb):
        return thumb, "frame", True
    # 抽帧也失败，清理可能生成的残次文件
    try:
        if os.path.isfile(thumb) and os.path.getsize(thumb) == 0:
            os.remove(thumb)
    except OSError:
        pass
    return None, None, False


def scan_videos(root):
    """递归收集目录下所有视频文件的完整路径（按路径排序）。"""
    found = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in sorted(filenames):
            if os.path.splitext(fn)[1].lower() in VIDEO_EXTS:
                found.append(os.path.join(dirpath, fn))
    return found
