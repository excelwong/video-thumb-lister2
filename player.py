#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本机视频播放器（VLC / PotPlayer）的查找与调用。"""

import os
import shutil
import subprocess

_POT_PORTABLE_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA", os.path.expanduser("~\\AppData\\Local")),
    "Programs", "PotPlayer")

VLC_CANDIDATES = [
    os.environ.get("VLC_BIN"),
    r"C:/Program Files/VideoLAN/VLC/vlc.exe",
    r"C:/Program Files (x86)/VideoLAN/VLC/vlc.exe",
    r"C:/VideoLAN/VLC/vlc.exe",
    r"D:/Program Files/VideoLAN/VLC/vlc.exe",
    "vlc",
]

POTPLAYER_CANDIDATES = [
    # 便携版（优先 mini 64 位）
    os.path.join(_POT_PORTABLE_DIR, "PotPlayerMini64.exe"),
    os.path.join(_POT_PORTABLE_DIR, "PotPlayer64.exe"),
    # 常见安装位置
    r"C:/Program Files/PotPlayer/PotPlayerMini64.exe",
    r"C:/Program Files/PotPlayer/PotPlayer64.exe",
    r"C:/Program Files/PotPlayer/PotPlayer.exe",
    r"C:/Program Files (x86)/PotPlayer/PotPlayer.exe",
    r"C:/PotPlayer/PotPlayer.exe",
    r"D:/Program Files/PotPlayer/PotPlayerMini64.exe",
    "PotPlayerMini64.exe",
    "PotPlayer64.exe",
    "PotPlayer.exe",
]

# 带空格/特殊字符的路径在命令行里要加引号（VLC 对某些字符敏感）
_POT_FILES = ("PotPlayerMini64.exe", "PotPlayer64.exe", "PotPlayer.exe")


def _first_existing(cands):
    for c in cands:
        if c and os.path.isfile(c):
            return c
    return None


def find_vlc():
    """返回 VLC 可执行文件路径，找不到返回 None。"""
    p = _first_existing(VLC_CANDIDATES)
    if p:
        return p
    return shutil.which("vlc")


def find_potplayer():
    """返回 PotPlayer 可执行文件路径，找不到返回 None。"""
    p = _first_existing(POTPLAYER_CANDIDATES)
    if p:
        return p
    for name in _POT_FILES:
        w = shutil.which(name)
        if w:
            return w
    # 便携版可能解压到带版本号的子目录，做一次递归兜底
    try:
        import glob as _glob
        base = os.path.dirname(_POT_PORTABLE_DIR)
        for name in _POT_FILES:
            hits = _glob.glob(os.path.join(base, "**", name), recursive=True)
            if hits:
                return hits[0]
    except Exception:  # noqa: BLE001
        pass
    return None


def list_players():
    """列出本机可用的播放器，返回 [{"key","name","path"}]。

    顺序：PotPlayer -> VLC（PotPlayer 启动快、对长文件名/字幕支持更好）。
    """
    found = []
    pot = find_potplayer()
    if pot:
        found.append({"key": "potplayer", "name": "PotPlayer", "path": pot})
    vlc = find_vlc()
    if vlc:
        found.append({"key": "vlc", "name": "VLC", "path": vlc})
    return found


def play(video_path, player_path=None):
    """用指定播放器打开视频。

    player_path 为空时自动挑一个可用播放器。
    返回 (ok, message)。
    """
    if not video_path or not os.path.isfile(video_path):
        return False, "文件不存在：%s" % video_path

    if not player_path:
        players = list_players()
        if not players:
            return False, "未找到 VLC 或 PotPlayer，请在设置里手动指定播放器路径。"
        player_path = players[0]["path"]

    if not os.path.isfile(player_path):
        # 允许传 "vlc" / "PotPlayerMini64.exe" 这类 PATH 上的名字
        w = shutil.which(player_path)
        if not w:
            return False, "播放器不存在：%s" % player_path
        player_path = w

    try:
        # 不等待播放器退出（Popen）；工作目录设到视频所在目录，
        # 便于播放器顺带加载同目录字幕。
        subprocess.Popen([player_path, os.path.abspath(video_path)],
                         cwd=os.path.dirname(os.path.abspath(video_path)))
        return True, os.path.basename(player_path)
    except Exception as e:  # noqa: BLE001
        return False, "启动播放器失败：%s" % e
