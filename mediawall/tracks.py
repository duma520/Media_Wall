"""按音轨转封装缓存：ffmpeg 把「视频 + 指定音轨」转封装成可流式播放的文件。

背景
----
浏览器原生无法切换 MKV/MP4 多音轨（桌面端 audioTracks 只读、移动端不支持），
所以 Media_Wall 采用服务端方案：用户选音轨 → 用 ffmpeg ``-map 0:v:0 -map 0:a:N
-c:v copy -c:a copy`` 把原文件转封装成**只含该音轨**的缓存文件 → 前端加载
``/media/<路径>?track=N`` 时直接流式输出缓存文件（支持 Range seek / 进度条）。

为什么是全量转封装而不是按 Range 动态转
--------------------------------------
Range 流要求响应内容**确定性**（同一起止区间永远返回相同字节），而 ffmpeg 转封装
输出的字节流依赖转封装起点。若每次请求都从头转、按请求的 Range 切，seek 到不同
位置会得到不同的文件头（mkv 头/簇结构随起始位置变化），浏览器解析会错乱。
所以先转出**完整缓存文件**，之后所有 Range 请求都从缓存文件读——确定且快。

方案 A（本模块实现）：预转 + 状态轮询
-------------------------------------
* 打开视频 → 先播**原文件**（默认音轨，秒开不等待）；
* 前端拿到记忆的音轨选择 → 调 ``start_build`` 后台预转（异步线程）；
* 前端轮询 ``build_status``（ready / building / error）→ ready 后把
  ``<video>`` 切到 ``?track=N`` 并恢复播放位置；
* 缓存就绪后零等待，seek/进度条全程正常（完整缓存文件可寻址）。

性能
----
视频/音频均 ``copy``（不重编码），转封装速度实测约 80x（248MB 文件 5 秒，
3.6GB 大文件约 1 分钟）。首次切音轨有一次性等待，之后走缓存零等待。

缓存位置与清理
------------
缓存文件在 ``<程序目录>/data/trackcache/``，文件名 = ``<hash>_t<N><原扩展名>``
（hash 基于文件绝对路径；扩展名跟随原文件，保证容器兼容性与原文件一致）。
条目按「最近使用」清理：总大小超 ``MAX_CACHE_BYTES``（默认 4GB）时删除最久
未用的缓存文件。

并发
----
同一 (文件, 音轨) 的转封装用 ``_building`` 集合串行化：重复 ``start_build``
不会拉起第二个进程；转封装失败记入 ``_failed``，``build_status`` 返回 error。
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from .common import PROGRAM_DIR, no_window
from .ffprobe import _find_ffprobe

#: 缓存目录（程序目录 data/ 下）
CACHE_DIR = PROGRAM_DIR / "data" / "trackcache"
#: 缓存总大小上限（默认 4GB，超出清最久未用的）
MAX_CACHE_BYTES = 4 * 1024 * 1024 * 1024
#: 转封装超时（秒）。3.6GB 文件 copy 转封装实测约 1 分钟，给 15 分钟余量。
_TRANSCODE_TIMEOUT = 15 * 60


def _ffmpeg_path() -> str | None:
    """定位 ffmpeg（与 ffprobe 同目录）；找不到返回 None。"""
    ffprobe = _find_ffprobe()
    if not ffprobe:
        return None
    p = Path(ffprobe)
    cand = p.with_name("ffmpeg.exe")
    if cand.is_file():
        return str(cand)
    return None


#: 转封装完成记录：(cache_path) -> (完成时刻, 大小)
_done: dict[Path, tuple[float, int]] = {}
#: 转封装失败记录：(cache_path) -> True
_failed: set[Path] = set()
#: 转封装进行中标记：(cache_path) -> True；避免并发重复转
_building: set[Path] = set()
_done_lock = threading.Lock()
_building_lock = threading.Lock()


def _cache_name(target: Path, track: int) -> Path:
    """缓存文件名：目标绝对路径 hash + 音轨号 + **原扩展名**（容器兼容）。"""
    digest = hashlib.sha1(str(target).encode("utf-8")).hexdigest()[:16]
    ext = target.suffix.lower() or ".mkv"
    return CACHE_DIR / f"{digest}_t{track}{ext}"


def get_ready(target: Path, track: int) -> Path | None:
    """缓存已就绪则返回缓存路径（并刷新 LRU 时间），否则 None（不触发转封装）。"""
    if not target.is_file():
        return None
    cache = _cache_name(target, track)
    if cache.is_file() and cache.stat().st_size > 0:
        _touch(cache)
        return cache
    return None


def start_build(target: Path, track: int) -> None:
    """异步触发转封装（幂等：已就绪/构建中/无 ffmpeg 时不重复）。

    立即返回，真正转封装在后台线程执行；前端轮询 ``build_status``。
    """
    if not target.is_file():
        return
    cache = _cache_name(target, track)
    if get_ready(target, track) is not None:
        return
    ffmpeg = _ffmpeg_path()
    if not ffmpeg:
        return
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    with _building_lock:
        if cache in _building:
            return
        _building.add(cache)
    threading.Thread(
        target=_build_worker, args=(ffmpeg, target, track, cache),
        name="mw-track-build", daemon=True,
    ).start()


def _build_worker(ffmpeg: str, target: Path, track: int, cache: Path) -> None:
    try:
        ok = _transcode(ffmpeg, target, track, cache)
        with _done_lock:
            if ok and cache.is_file():
                _done[cache] = (time.time(), cache.stat().st_size)
            elif not ok:
                _failed.add(cache)
        if ok:
            _enforce_limit()
            _touch(cache)
    finally:
        with _building_lock:
            _building.discard(cache)


def build_status(target: Path, track: int) -> str:
    """查询转封装状态：ready / building / error / not_started。"""
    if get_ready(target, track) is not None:
        return "ready"
    with _done_lock:
        if _cache_name(target, track) in _failed:
            return "error"
    with _building_lock:
        if _cache_name(target, track) in _building:
            return "building"
    return "not_started"


def _transcode(ffmpeg: str, target: Path, track: int, cache: Path) -> bool:
    """执行一次转封装：视频复制 + 指定音频流复制 → 缓存文件。"""
    # 临时文件：转封装中途崩溃不留下半截缓存（原子写思想）。
    # 注意：临时文件**必须保留合法扩展名**（ffmpeg 按扩展名识别输出容器，
    # 之前用 `.mp4.tmp` 导致 "Unable to choose an output format"）。
    tmp = cache.with_name(cache.stem + ".part" + cache.suffix)
    try:
        tmp.unlink(missing_ok=True)
    except OSError:
        pass
    cmd = [
        ffmpeg, "-y",
        "-i", str(target),
        "-map", "0:v:0",
        "-map", f"0:a:{track}",
        "-c:v", "copy", "-c:a", "copy",
        "-sn",           # 去掉字幕（音轨选择场景不需要，且字幕流复制会拖慢）
        str(tmp),
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=_TRANSCODE_TIMEOUT, errors="replace",
            creationflags=no_window(),
        )
    except (OSError, subprocess.TimeoutExpired):
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False
    if proc.returncode != 0 or not tmp.is_file() or tmp.stat().st_size <= 0:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False
    try:
        os.replace(tmp, cache)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False
    return True


def _touch(cache: Path) -> None:
    """更新缓存文件访问时间（用于 LRU 清理）。"""
    try:
        now = time.time()
        os.utime(cache, (now, now))
    except OSError:
        pass


def _enforce_limit() -> None:
    """缓存总大小超限时，删除最久未用的音轨缓存文件。"""
    try:
        files = [p for p in CACHE_DIR.glob("*_t*.mkv") if p.is_file()]
        total = sum(p.stat().st_size for p in files)
        if total <= MAX_CACHE_BYTES:
            return
        files.sort(key=lambda p: p.stat().st_mtime)
        for p in files:
            if total <= MAX_CACHE_BYTES * 0.8:
                break
            try:
                size = p.stat().st_size
                p.unlink()
                total -= size
            except OSError:
                pass
    except OSError:
        pass


def clear_cache() -> int:
    """清空音轨缓存，返回删除的文件数（供设置页/诊断用）。"""
    removed = 0
    try:
        for p in CACHE_DIR.glob("*_t*.mkv"):
            try:
                p.unlink()
                removed += 1
            except OSError:
                pass
    except OSError:
        pass
    return removed


__all__ = [
    "get_ready", "start_build", "build_status", "clear_cache",
    "CACHE_DIR", "MAX_CACHE_BYTES",
]
