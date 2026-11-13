"""音轨探测：用 ffprobe 列出视频文件的音频流（供前端音轨选择）。

为什么需要这个模块
------------------
浏览器原生 ``<video>.audioTracks`` 在桌面端是只读的（改不了 enabled），
移动端（安卓 Chromium / 鸿蒙 ArkWeb）根本不支持。所以 Media_Wall 的多音轨
选择走「服务端按音轨转封装」：先探测文件里有几条音频流（语言/编码/声道），
前端弹出选择，用户选定后请求 ``/media/<路径>?track=N``，服务端用 ffmpeg
把该音轨转封装成可流式播放的缓存文件（见 ``tracks.py``）。

ffprobe 路径
------------
优先取环境变量 ``FFPROBE_PATH``；其次取 ``ffprobe``（PATH）；最后尝试
本机常见安装位置（D:\\ffmpeg\\bin\\ffprobe.exe）。探测不到时返回空列表，
前端不显示音轨按钮（功能降级，不影响播放）。

缓存
----
同一文件的音轨清单缓存 60 秒（音轨是文件固有属性，短时间内不会变），
避免每次打开弹窗都跑一次 ffprobe（大文件在 NAS 上探测也要几百毫秒）。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from .common import no_window

#: 探测结果缓存：(绝对路径) -> (探测时刻, 音频流列表)
_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_cache_lock = threading.Lock()
#: 音轨清单缓存时长（秒）
_CACHE_SECONDS = 60.0
#: 探测超时（秒）。NAS 上的大文件 ffprobe 读头也要时间，给足余量。
_PROBE_TIMEOUT = 15.0

#: 常见 ffprobe 安装位置（按顺序尝试）
_FFPROBE_CANDIDATES = (
    r"D:\ffmpeg\bin\ffprobe.exe",
    r"C:\ffmpeg\bin\ffprobe.exe",
)


def _find_ffprobe() -> str | None:
    """定位 ffprobe 可执行文件；找不到返回 None。"""
    import os

    env = os.environ.get("FFPROBE_PATH", "").strip().strip('"')
    if env:
        return env
    found = shutil.which("ffprobe")
    if found:
        return found
    for cand in _FFPROBE_CANDIDATES:
        if Path(cand).is_file():
            return cand
    return None


def _parse_tracks(text: str) -> list[dict[str, Any]]:
    """解析 ffprobe JSON 输出 → 音频流列表。

    返回形如 ``[{index, codec, language, title, channels}, ...]``。
    ``index`` 是 ffmpeg 的音频流序号（``-map 0:a:<index>`` 用的就是这个）。
    """
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    streams = data.get("streams") or []
    out: list[dict[str, Any]] = []
    audio_no = 0
    for st in streams:
        if st.get("codec_type") != "audio":
            continue
        tags = st.get("tags") or {}
        out.append({
            "index": audio_no,  # 音频流序号（ffmpeg -map 0:a:N 用）
            "codec": st.get("codec_name") or "",
            "language": str(tags.get("language") or ""),
            "title": str(tags.get("title") or ""),
            "channels": int(st.get("channels") or 0),
        })
        audio_no += 1
    return out


def probe_tracks(path: Path) -> list[dict[str, Any]]:
    """返回文件的音频流列表（带缓存）。失败/无 ffprobe 时返回空列表。"""
    key = str(path)
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit is not None and now - hit[0] < _CACHE_SECONDS:
            return hit[1]
    ffprobe = _find_ffprobe()
    if not ffprobe:
        return []
    try:
        proc = subprocess.run(
            [ffprobe, "-v", "error",
             "-select_streams", "a",
             "-show_entries", "stream=index,codec_type,codec_name,channels",
             "-show_entries", "stream_tags=language,title",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=_PROBE_TIMEOUT,
            errors="replace", creationflags=no_window(),
        )
        tracks = _parse_tracks(proc.stdout or "")
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return []
    with _cache_lock:
        _cache[key] = (time.time(), tracks)
        # 防止字典无限膨胀：超过 2000 条时清掉最旧的
        if len(_cache) > 2000:
            oldest = sorted(_cache.items(), key=lambda kv: kv[1][0])[:500]
            for k, _ in oldest:
                _cache.pop(k, None)
    return tracks


def track_label(track: dict[str, Any], idx: int) -> str:
    """音轨的展示名：优先 title，其次「语言 + 编码」，最后「音轨 N」。

    语言代码转常见中文名（ger→德语、eng→英语、chi/zho→国语、yue→粤语…），
    未知代码原样显示。
    """
    title = str(track.get("title") or "").strip()
    if title:
        return title
    lang = str(track.get("language") or "").strip().lower()
    lang_name = _LANG_NAMES.get(lang, lang or "")
    codec = str(track.get("codec") or "")
    parts = [p for p in (lang_name, codec.upper()) if p]
    label = " / ".join(parts) if parts else f"音轨 {idx + 1}"
    ch = int(track.get("channels") or 0)
    if ch:
        label += f" · {ch} 声道"
    return label


#: 常见语言代码 → 中文名（够用即可，未覆盖的显示原代码）
_LANG_NAMES = {
    "chi": "国语", "zho": "国语", "cmn": "国语", "zh": "国语",
    "yue": "粤语", "cantonese": "粤语",
    "eng": "英语", "en": "英语",
    "ger": "德语", "de": "德语", "deu": "德语",
    "jpn": "日语", "ja": "日语", "jp": "日语",
    "kor": "韩语", "ko": "韩语",
    "fre": "法语", "fr": "法语", "fra": "法语",
    "spa": "西班牙语", "es": "西班牙语",
    "ita": "意大利语", "it": "意大利语",
    "rus": "俄语", "ru": "俄语",
    "tha": "泰语", "th": "泰语",
    "vie": "越南语", "vi": "越南语",
    "por": "葡萄牙语", "pt": "葡萄牙语",
    "ara": "阿拉伯语",
    "hin": "印地语",
    "und": "未知语言",
}


__all__ = ["probe_tracks", "track_label", "_find_ffprobe"]
