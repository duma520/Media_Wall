"""播放决策与按需转换缓存（v2.9.0）。

背景
----
浏览器能原生解的容器/编码很有限（mp4/mov/webm + h264/vp8/vp9 视频、
mp3/aac/flac/wav/ogg 音频；桌面 Edge 额外能播 mkv 容器，安卓不能）。
库内实测（_scaffold/_codec_probe_report.md，ffprobe 抽样 239 个 + 真实播放验证）：

* .ts / .flv / .m2ts：里面基本就是 h264+aac，**容器打不开**，
  ``-c copy`` 转封装成 mp4 即可播（实测 50~570x 实时，2GB 约 55 秒）；
* .mkv：h264 片桌面 Edge 可直放、安卓不行 → 前端先试原文件，失败再转封装；
  hevc 部分（10bit/8bit）本机无硬解，**转封装也救不了** → 系统播放器兜底；
* .avi / .wmv / .rmvb：编码多为 wmv3/mpeg4/rv40，浏览器与 mp4 容器都不收 →
  判定 fail，直接提示用系统播放器；
* 音频 .wma/.ape/.wv/.tta/.aiff/.amr/.mka：浏览器播不了 → 服务端 ``libmp3lame``
  转 mp3（本机实测 ~7~13x 实时，一首 3 分钟约 20~30 秒）；
  mp3/m4a/flac/wav/ogg 直放不转。

设计
----
* **先分析后转换**：``/api/playinfo?path=`` 用 ffprobe 判码，给出
  decision = direct / remux / remux_audio / transcode_audio / fail / unknown，
  前端据此决定「直接播 / 转换后播 / 提示系统播放器」，不再黑屏干等。
* **能 copy 绝不重编码**：remux 只换容器；只有音频编码不可用才重编音频
  （视频重编在本机 0.0596x，永远不做）。
* **全量转 + 状态轮询**（沿用 tracks.py 的结论）：Range 流要求字节确定性，
  不能边转边发。转换在后台线程，进度解析 ffmpeg ``-progress`` 的
  out_time_us，前端轮询 ``/api/playinfo`` 拿 pct 画进度条，ready 后切
  ``/media/<路径>?fmt=<mp4|mp3>`` 流式缓存文件。
* **缓存独立**：``data/playcache/``（与音轨 trackcache 分开互不干扰），
  文件名 = ``<abs路径hash>_<模式><mp4|mp3>``；LRU 上限 6GB；并发转换
  信号量限 2 路（本机 CPU 弱，防互相拖死）。
* **降级安全**：找不到 ffprobe/ffmpeg → decision=unknown / start 失败，
  前端回退旧行为（试原文件 + 系统播放器），绝不抛错、绝不阻塞播放路径。
  编译态 dist 不打包 ffmpeg（用户决策），无 ffmpeg 的机器上功能自动隐身。
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any

from .common import PROGRAM_DIR, no_window
from .ffprobe import _find_ffprobe
from .media import PLAYABLE_EXTS as _PLAYABLE_EXTS
from .tracks import _ffmpeg_path

#: 转换缓存目录（与 trackcache 平级、互不影响）
CACHE_DIR = PROGRAM_DIR / "data" / "playcache"
#: 缓存总大小上限（转封装 mp4 与原文件同量级；6GB 约容 3~4 部 2 小时电影）
MAX_CACHE_BYTES = 6 * 1024 * 1024 * 1024
#: ffprobe 分析超时（秒）
_PROBE_TIMEOUT = 20.0
#: 分析结果缓存时长（秒）；比音轨探测长，编码分布是文件固有属性
_PROBE_TTL = 600.0
#: copy 转封装超时（3.6GB 实测约 60 秒，给 20 分钟余量）
_COPY_TIMEOUT = 20 * 60
#: 音频重编码超时（本机 mp3 编码 ~7x 实时，2 小时有声书上限约 20 分钟）
_ENCODE_TIMEOUT = 60 * 60
#: 同时转换数上限（CPU Phenom II X4 955，两路已到吞吐极限）
MAX_CONCURRENT_BUILDS = 2

#: 浏览器可解码的视频编码（hevc/vc1/wmv3/rv40/mpeg2 不在内——救不了）
_V_BROWSER = frozenset({"h264", "vp8", "vp9"})
#: 可以 ``copy`` 进 mp4 且主流浏览器能解的音频编码。
#: ac3/eac3 刻意不在内：mp4 里塞 ac3 后安卓 WebView/部分浏览器解不了，
#: 而重编 mp3 是保序可播的最低成本方案（remux_audio 分支）。
_A_MP4_COPY = frozenset({"aac", "mp3", "flac", "opus"})
#: 浏览器能直解的裸音频编码（容器得对；wav/flac/mp3/ogg 原生容器即此组）
_A_BROWSER = frozenset({"mp3", "aac", "flac", "vorbis", "opus",
                        "pcm_s16le", "pcm_s24le", "pcm_f32le", "alac"})
#: 视频原生容器（浏览器直接收）
_V_NATIVE_CONT = frozenset({".mp4", ".m4v", ".mov", ".webm"})
#: 音频原生容器
_A_NATIVE_CONT = frozenset({".mp3", ".m4a", ".wav", ".flac", ".ogg",
                            ".opus", ".aac", ".m4b"})

#: 需要（且能）转换的 decision 集合
ACTIONABLE = frozenset({"remux", "remux_audio", "transcode_audio"})

# ---------------------------------------------------------------------------
# 状态表（进程内；缓存文件本身跨重启有效，get_ready 直接查文件）
# ---------------------------------------------------------------------------
_probe_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_probe_lock = threading.Lock()
_done: set[Path] = set()
_failed: dict[Path, str] = {}          # cache -> ffmpeg stderr 尾部
_building: set[Path] = set()
_pct: dict[Path, float] = {}           # cache -> 0..99
_lock = threading.Lock()
_sem = threading.Semaphore(MAX_CONCURRENT_BUILDS)


# ---------------------------------------------------------------------------
# 分析
# ---------------------------------------------------------------------------

def _probe_streams(target: Path) -> dict[str, Any] | None:
    """ffprobe 一次拿全部流 + 时长。失败/无 ffprobe 返回 None。"""
    ff = _find_ffprobe()
    if not ff:
        return None
    try:
        proc = subprocess.run(
            [ff, "-v", "error",
             "-show_entries",
             "stream=codec_type,codec_name,pix_fmt:format=duration",
             "-of", "json", str(target)],
            capture_output=True, text=True, timeout=_PROBE_TIMEOUT,
            errors="replace", creationflags=no_window(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout or "{}")
    except ValueError:
        return None
    videos: list[dict[str, Any]] = []
    audios: list[str] = []
    v_no = 0
    for st in data.get("streams") or []:
        if st.get("codec_type") == "video":
            # idx = ffmpeg 视频流序号（-map 0:v:idx），封面流可能排在正片前
            videos.append({"codec": str(st.get("codec_name") or ""),
                           "pix_fmt": str(st.get("pix_fmt") or ""),
                           "idx": v_no})
            v_no += 1
        elif st.get("codec_type") == "audio":
            audios.append(str(st.get("codec_name") or ""))
    dur_raw = (data.get("format") or {}).get("duration")
    try:
        duration = float(dur_raw)
    except (TypeError, ValueError):
        duration = None
    return {"videos": videos, "audios": audios, "duration": duration}


def analyze(target: Path) -> dict[str, Any] | None:
    """带 TTL 缓存的 ffprobe 分析（键 = 绝对路径）。"""
    key = str(target)
    now = time.time()
    with _probe_lock:
        hit = _probe_cache.get(key)
        if hit is not None and now - hit[0] < _PROBE_TTL:
            return hit[1]
    info = _probe_streams(target)
    if info is None:
        return None
    with _probe_lock:
        _probe_cache[key] = (time.time(), info)
        if len(_probe_cache) > 3000:
            for k in sorted(_probe_cache, key=lambda k: _probe_cache[k][0])[:800]:
                _probe_cache.pop(k, None)
    return info


def decide(target: Path) -> dict[str, Any]:
    """给出该文件的播放决策（不触发转换）。

    返回 ``{decision, reason, vcodec, acodecs, duration, out_ext}``；
    decision ∈ direct / remux / remux_audio / transcode_audio / fail / unknown。

    音视频角色**按扩展名定**，不按流表：flac/m4a 里内嵌封面（ffprobe 会列成
    video 流，实测 01.エピソード1….flac 带 mjpeg 封面）不能把它当视频判；
    反过来 mkv 里的 mjpeg 缩略图流也忽略，取第一条真视频。
    """
    from .common import AUDIO_EXTS, VIDEO_EXTS

    ext = target.suffix.lower()
    info = analyze(target)
    if info is None:
        return {"decision": "unknown", "reason": "ffprobe 不可用或分析失败",
                "vcodec": "", "acodecs": [], "duration": None, "out_ext": None}
    v = info["videos"][0] if info["videos"] else None
    acodecs = [a for a in info["audios"] if a]
    base = {"vcodec": str((v or {}).get("codec") or ""),
            "acodecs": acodecs, "duration": info["duration"]}

    is_audio_file = ext in AUDIO_EXTS and ext not in VIDEO_EXTS
    if v is not None and not is_audio_file:
        # 选主视频流：优先非封面（mjpeg 常见于音频内嵌封面/缩略图）
        main = next((s for s in info["videos"]
                     if s["codec"] not in ("mjpeg", "png")), info["videos"][0])
        vc = main["codec"]
        base = {**base, "vcodec": vc, "vidx": main["idx"]}
        if vc not in _V_BROWSER:
            return {**base, "decision": "fail", "out_ext": None,
                    "reason": f"视频编码 {vc} 浏览器与本机均无法解码/转码"}
        if ext in _V_NATIVE_CONT:
            return {**base, "decision": "direct", "out_ext": None,
                    "reason": "浏览器可直放"}
        if all(a in _A_MP4_COPY for a in acodecs):
            return {**base, "decision": "remux", "out_ext": ".mp4",
                    "reason": "换 mp4 容器即可（不重编码）"}
        return {**base, "decision": "remux_audio", "out_ext": ".mp4",
                "reason": "视频复制 + 音频转 mp3（音频编码浏览器不认）"}

    # 到这里：音频文件，或「视频扩展名但其实只有音频流」（如 mkv 纯广播剧）
    if not acodecs:
        return {**base, "decision": "fail", "out_ext": None,
                "reason": "没有可播的视频/音频流"}
    if ext in _A_NATIVE_CONT and all(a in _A_BROWSER for a in acodecs):
        return {**base, "decision": "direct", "out_ext": None,
                "reason": "浏览器可直放"}
    return {**base, "decision": "transcode_audio", "out_ext": ".mp3",
            "reason": "音频转 mp3（浏览器不支持该编码/容器）"}


# ---------------------------------------------------------------------------
# 缓存与转换
# ---------------------------------------------------------------------------

def _mode(decision: str) -> str:
    return {"remux": "rv", "remux_audio": "ra",
            "transcode_audio": "ta"}.get(decision, "rx")


def _cache_path(target: Path, decision: str) -> Path:
    digest = hashlib.sha1(str(target).encode("utf-8")).hexdigest()[:16]
    out = ".mp3" if decision == "transcode_audio" else ".mp4"
    return CACHE_DIR / f"{digest}_{_mode(decision)}{out}"


def get_ready(target: Path) -> Path | None:
    """缓存已就绪返回路径（并 LRU touch）；否则 None。"""
    d = decide(target)
    if d["decision"] not in ACTIONABLE:
        return None
    cache = _cache_path(target, d["decision"])
    if cache.is_file() and cache.stat().st_size > 0:
        _touch(cache)
        return cache
    return None


def start_build(target: Path) -> bool:
    """异步触发转换（幂等）。无 ffmpeg / 不可救返回 False。"""
    d = decide(target)
    if d["decision"] not in ACTIONABLE:
        return False
    cache = _cache_path(target, d["decision"])
    if cache.is_file() and cache.stat().st_size > 0:
        return True
    ffmpeg = _ffmpeg_path()
    if not ffmpeg:
        return False
    with _lock:
        if cache in _building or cache in _done or cache in _failed:
            # failed 也直接返回：避免每次轮询都重试打爆 CPU；
            # 用户想重来可点设置页「清空转换缓存」或重启服务。
            return True
        _building.add(cache)
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        with _lock:
            _building.discard(cache)
        return False
    threading.Thread(
        target=_build_worker, args=(ffmpeg, target, d, cache),
        name="mw-play-build", daemon=True,
    ).start()
    return True


def _build_cmd(ffmpeg: str, target: Path, decision: str, tmp: Path,
               vidx: int = 0) -> list[str]:
    base = [ffmpeg, "-y", "-v", "error", "-progress", "pipe:1", "-nostats",
            "-i", str(target)]
    if decision == "transcode_audio":
        # mp3 容器只允许一条音频流：取第一条（多音轨场景极少见）
        base += ["-vn", "-map", "0:a:0", "-c:a", "libmp3lame", "-b:a", "192k"]
    else:
        base += ["-map", f"0:v:{vidx}", "-map", "0:a?"]
        if decision == "remux":
            base += ["-c:v", "copy", "-c:a", "copy"]
        else:  # remux_audio：视频 copy，音频重编 mp3
            base += ["-c:v", "copy", "-c:a", "libmp3lame", "-b:a", "192k"]
        base += ["-sn", "-dn", "-movflags", "+faststart"]
    base.append(str(tmp))
    return base


def _set_pct(cache: Path, p: float) -> None:
    with _lock:
        _pct[cache] = p


def _build_worker(ffmpeg: str, target: Path, d: dict[str, Any], cache: Path) -> None:
    decision = d["decision"]
    try:
        _sem.acquire()
        try:
            ok = _transcode(ffmpeg, target, decision, d, cache)
        finally:
            _sem.release()
        with _lock:
            if ok and cache.is_file():
                _done.add(cache)
            else:
                _failed.setdefault(
                    cache, "转换失败（详见服务端日志/ffmpeg stderr 尾部）")
            _pct.pop(cache, None)
        if ok:
            _touch(cache)
            _enforce_limit()
    finally:
        with _lock:
            _building.discard(cache)


def _transcode(ffmpeg: str, target: Path, decision: str,
               d: dict[str, Any], cache: Path) -> bool:
    # 临时文件必须保留合法扩展名（ffmpeg 按扩展名选容器，tracks.py 的教训）
    tmp = cache.with_name(cache.stem + ".part" + cache.suffix)
    try:
        tmp.unlink(missing_ok=True)
    except OSError:
        pass
    cmd = _build_cmd(ffmpeg, target, decision, tmp,
                     int(d.get("vidx") or 0))
    # copy 转封装只受 IO 限制；带 mp3 重编码的按 CPU 速度给足余量
    timeout = _COPY_TIMEOUT if decision == "remux" else _ENCODE_TIMEOUT
    duration = d.get("duration")
    try:
        # stderr 并入 stdout：单独 PIPE 时我们只逐行读 stdout，ffmpeg 报错
        # 多了会把 stderr 管道塞满而整体卡死（进度条永远不动的假死根因）。
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, errors="replace", creationflags=no_window(),
        )
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False
    assert proc.stdout is not None
    started = time.time()
    timed_out = False
    tail: list[str] = []           # 输出末尾若干行，失败时作为 error_detail
    try:
        for line in proc.stdout:
            line = line.strip()
            if line.startswith("out_time=") or line.startswith(
                    ("out_time_us=", "out_time_ms=")):
                # 注意：ffmpeg 的 out_time_ms 实际也是微秒（历史遗留同名值）
                if duration and duration > 0:
                    try:
                        us = int(line.split("=", 1)[1])
                    except ValueError:
                        us = -1
                    if us >= 0:
                        _set_pct(cache, min(99.0, us / 1e6 / duration * 100.0))
            elif line:
                tail.append(line)
                if len(tail) > 40:
                    tail.pop(0)
            if time.time() - started > timeout:
                timed_out = True
                break
        if timed_out:
            proc.kill()
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
    except OSError:
        proc.kill()
        timed_out = True
    err = "\n".join(tail)[-400:]
    if proc.returncode != 0 or timed_out or not tmp.is_file() \
            or tmp.stat().st_size <= 0:
        if err:
            with _lock:
                _failed[cache] = ("超时" if timed_out else "") + err
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


def build_status(target: Path) -> str:
    """ready / building / error / not_started（direct/fail 恒 not_started）。"""
    d = decide(target)
    if d["decision"] not in ACTIONABLE:
        return "not_started"
    cache = _cache_path(target, d["decision"])
    if cache.is_file() and cache.stat().st_size > 0:
        _touch(cache)
        return "ready"
    with _lock:
        if cache in _failed:
            return "error"
        if cache in _building:
            return "building"
    return "not_started"


def build_pct(target: Path) -> float | None:
    d = decide(target)
    if d["decision"] not in ACTIONABLE:
        return None
    with _lock:
        return _pct.get(_cache_path(target, d["decision"]))


def build_error(target: Path) -> str:
    d = decide(target)
    if d["decision"] not in ACTIONABLE:
        return ""
    with _lock:
        return _failed.get(_cache_path(target, d["decision"]), "")


def _touch(cache: Path) -> None:
    try:
        now = time.time()
        os.utime(cache, (now, now))
    except OSError:
        pass


def _enforce_limit() -> None:
    """超上限删最久未用的转换缓存（LRU 按 atime/mtime，_touch 保活跃文件）。"""
    try:
        files = [p for p in CACHE_DIR.iterdir()
                 if p.is_file() and p.suffix in (".mp4", ".mp3")
                 and ".part" not in p.name]
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
                with _lock:
                    _done.discard(p)
            except OSError:
                pass
    except OSError:
        pass


def clear_cache() -> int:
    """清空转换缓存目录，返回删除文件数（设置页「清空转换缓存」用）。"""
    removed = 0
    try:
        for p in CACHE_DIR.iterdir():
            if p.is_file() and p.suffix in (".mp4", ".mp3"):
                try:
                    p.unlink()
                    removed += 1
                    with _lock:
                        _done.discard(p)
                        _failed.pop(p, None)
                        _pct.pop(p, None)
                except OSError:
                    pass
    except OSError:
        pass
    return removed


# ---------------------------------------------------------------------------
# HTTP：GET /api/playinfo?path=<相对路径>[&build=1]、POST /api/play/clear
# ---------------------------------------------------------------------------

def handle_api(handler: Any, path: str) -> bool:
    """处理 ``/api/playinfo`` 与 ``/api/play/clear``；非本路由返回 False。"""
    if path == "/api/play/clear":
        if handler.command != "POST":
            handler._send_json(405, {"ok": False, "error": "method not allowed, use POST"})
            return True
        if not handler._origin_allowed():
            handler._send_json(403, {"ok": False, "error": "origin not allowed"})
            return True
        handler._drain_body()
        handler._send_json(200, {"ok": True, "removed": clear_cache()})
        return True
    if path != "/api/playinfo":
        return False
    if handler.command not in ("GET", "HEAD"):
        handler._send_json(405, {"ok": False, "error": "method not allowed"})
        return True
    from .common import PathSecurityError, resolve_within

    qs = urllib.parse.parse_qs(urllib.parse.urlsplit(handler.path).query,
                               keep_blank_values=True)
    rel = (qs.get("path") or [None])[0]
    if not rel:
        handler._send_json(400, {"ok": False, "error": "need path"})
        return True
    try:
        target = resolve_within(handler._root, rel)
    except PathSecurityError:
        handler._send_json(403, {"ok": False, "error": "forbidden"})
        return True
    if target.suffix.lower() not in _PLAYABLE_EXTS or not target.is_file():
        handler._send_json(404, {"ok": False, "error": "not found or not playable"})
        return True
    d = decide(target)
    out: dict[str, Any] = {"ok": True, "path": rel}
    out.update(d)
    if d["decision"] in ACTIONABLE:
        if "build" in qs:
            start_build(target)
        out["status"] = build_status(target)
        if out["status"] == "error":
            out["error_detail"] = build_error(target)
        p = build_pct(target)
        if p is not None:
            out["pct"] = round(p, 1)
    handler._send_json(200, out)
    return True


def handle_tracks(handler: Any) -> None:
    """``GET /api/tracks?path=<相对路径>[&start=N]``（v2.5.0，v2.9.0 从 httpd 搬入）。

    音轨清单与转封装状态；可选 ``start=N`` 顺便触发音轨 N 的预转
    （幂等：已就绪/构建中不会重复转）。
    """
    from .common import PathSecurityError, resolve_within
    from .ffprobe import probe_tracks, track_label
    from .tracks import build_status as _track_status, start_build as _track_start

    qs = urllib.parse.parse_qs(urllib.parse.urlsplit(handler.path).query,
                               keep_blank_values=True)
    rel = (qs.get("path") or [None])[0]
    if not rel:
        handler._send_json(400, {"ok": False, "error": "need path"})
        return
    try:
        target = resolve_within(handler._root, rel)
    except PathSecurityError:
        handler._send_json(403, {"ok": False, "error": "forbidden"})
        return
    if not target.is_file() or target.suffix.lower() not in _PLAYABLE_EXTS:
        handler._send_json(404, {"ok": False, "error": "not found or not playable"})
        return

    tracks = probe_tracks(target)
    start_no = None
    if qs.get("start"):
        try:
            start_no = int(qs["start"][0])
        except (TypeError, ValueError):
            start_no = None
    if start_no is not None and 0 <= start_no < len(tracks):
        _track_start(target, start_no)

    out_tracks = []
    for idx, tr in enumerate(tracks):
        out_tracks.append({
            "index": idx,
            "label": track_label(tr, idx),
            "codec": tr.get("codec", ""),
            "language": tr.get("language", ""),
            "title": tr.get("title", ""),
            "channels": tr.get("channels", 0),
            "status": _track_status(target, idx),
        })
    handler._send_json(200, {
        "ok": True,
        "path": rel,
        "tracks": out_tracks,
        "has_multi_track": len(out_tracks) > 1,
    })


__all__ = [
    "decide", "analyze", "start_build", "get_ready", "build_status",
    "build_pct", "build_error", "clear_cache", "handle_api", "handle_tracks",
    "ACTIONABLE", "CACHE_DIR", "MAX_CACHE_BYTES",
]
