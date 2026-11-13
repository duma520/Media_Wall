"""媒体流与缩略图输出（由 AV_Wall v1.9.0 的 avwall/media.py 泛化）。

职责：把 ``/media``（视频/音频 Range 流）与 ``/thumb``（缩略图）两件事独立成模块，
让 ``httpd.py`` 保持在 1000 行以内（项目规则 5）。

与 AV_Wall 的差异：可播集合从「仅视频」扩为 ``VIDEO_EXTS ∪ AUDIO_EXTS``，
供「96 录音笔」等音频分区直接走同一条流式路由；新增 mkv/avi/ts 等扩展名的
MIME 映射（浏览器解码不了的，前端「系统播放器」兜底下载）。

设计要点（这些是实测踩过坑之后定下来的，不要随手改）：

* **先开文件再发头**。原先是「先承诺 ``Content-Length``，再 ``open()``」——
  文件在 stat 之后消失（NAS 抖动 / 文件被删）时，客户端会收到
  ``Content-Length: 65536`` 却收到 0 字节，且连接不关闭 → 客户端永久挂死。
* **读不满就强制断连**。读取中途被截断时，实际发出的字节数少于声明长度，
  必须 ``close_connection = True``，让客户端立刻感知「连接断了」而不是无限等待。
* **媒体流刻意不强制关闭连接**。安卓 / 鸿蒙（Chromium / ArkWeb）的媒体框架会在
  同一连接上连续发多个 Range 请求（seek / 续传）；每个 Range 都 ``Connection: close``
  会让部分手机在第一个分片后停止拉流，表现为「卡住 / 转圈」。

这些方法以 mixin 形式提供，``AvWallHandler`` 继承本 mixin 后即可直接调用
``self._serve_media`` / ``self._serve_thumb`` / ``self._stream`` /
``self._send_range_error``。
"""

from __future__ import annotations

import json
import mimetypes
import os
import urllib.parse
from pathlib import Path
from typing import Any

from .common import (
    AUDIO_EXTS,
    CHUNK_SIZE,
    MAX_THUMB_BYTES,
    THUMB_EXTS,
    VIDEO_EXTS,
    PathSecurityError,
    RangeNotSatisfiable,
    open_path,
    parse_range_header,
    resolve_within,
    stat_path,
)

#: 可在线流式传输的扩展名（视频 + 音频）。
PLAYABLE_EXTS = VIDEO_EXTS | AUDIO_EXTS

#: 显式 MIME 映射：避免某些系统把 .mov/.m4v/.mkv 等猜成
#: application/octet-stream，导致安卓 / 鸿蒙媒体栈拒绝播放。
_MEDIA_MIME = {
    ".mp4": "video/mp4", ".m4v": "video/mp4",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
    ".mkv": "video/x-matroska",
    ".avi": "video/x-msvideo",
    ".ts": "video/mp2t",
    ".flv": "video/x-flv",
    ".wmv": "video/x-ms-wmv",
    # v2.9.0 扩容（与 common.VIDEO_EXTS/AUDIO_EXTS 同源）
    ".m2ts": "video/mp2t", ".mts": "video/mp2t",
    ".rmvb": "application/vnd.rn-realmedia-vbr", ".rm": "application/vnd.rn-realmedia",
    ".mpg": "video/mpeg", ".mpeg": "video/mpeg", ".m2v": "video/mpeg",
    ".vob": "video/msvideo", ".3gp": "video/3gpp", ".3g2": "video/3gpp2",
    ".asf": "video/x-ms-asf", ".f4v": "video/mp4", ".divx": "video/divx",
    ".ogv": "video/ogg",
    ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".wav": "audio/wav",
    ".flac": "audio/flac", ".aac": "audio/aac", ".ogg": "audio/ogg",
    ".opus": "audio/ogg", ".wma": "audio/x-ms-wma",
    ".mka": "audio/x-matroska", ".m4b": "audio/mp4",
    ".ape": "audio/ape", ".wv": "audio/wavpack", ".tta": "audio/tta",
    ".aif": "audio/aiff", ".aiff": "audio/aiff", ".amr": "audio/amr",
}


class MediaMixin:
    """媒体与缩略图输出（供 :class:`avwall.httpd.AvWallHandler` 继承）。"""

    # ---- 类型声明（由使用方 AvWallHandler 提供实现）----
    _root: Path

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        raise NotImplementedError

    def _send_bytes(self, status: int, content_type: str, body: bytes) -> None:
        raise NotImplementedError

    def _cors_header_value(self) -> str | None:
        raise NotImplementedError

    def _send_range_error(self, status: int, size: int) -> None:
        raise NotImplementedError

    # ---- 路由实现 ----

    def _serve_media(self, rel: str) -> None:
        """``GET /media/<相对路径>``：按 Range 头流式返回视频。

        支持查询参数 ``?track=N``（v2.5.0 音轨选择）：
        * 无 track → 直接流式原文件（默认音轨）；
        * 有 track → 若音轨缓存已就绪，流式输出**缓存文件**（Range 确定性）；
          若还没转好，返回 202 ``{"status":"building"}``，前端继续轮询
          ``/api/tracks?path=...`` 直到 ready 再切换。

        支持查询参数 ``?fmt=mp4|mp3``（v2.9.0 转封装/转码播放）：
        输出 :mod:`mediawall.play` 的转换缓存文件（同样 Range 确定性）；
        转换中 202 building，未触发/失败 409（前端据此决定重试或兜底）。
        """
        # 拆 query：rel 可能形如 "01 电影/xxx.mp4?track=1"
        query = ""
        if "?" in rel:
            rel, query = rel.split("?", 1)
        track: int | None = None
        fmt: str | None = None
        if query:
            try:
                qs = urllib.parse.parse_qs(query)
                if qs.get("track"):
                    track = int(qs["track"][0])
                if qs.get("fmt"):
                    fmt = qs["fmt"][0].lower()
            except (ValueError, TypeError):
                track = None
                fmt = None

        try:
            target = resolve_within(self._root, rel)
        except PathSecurityError:
            self._send_json(403, {"error": "forbidden"})
            return
        if target.suffix.lower() not in PLAYABLE_EXTS:
            self._send_json(403, {"error": "file type not allowed"})
            return

        # v2.9.0：fmt 模式 → 换流转封装/转码缓存（与音轨缓存互不干扰）
        if fmt in ("mp4", "mp3") and track is None:
            from . import play

            d = play.decide(target)
            if d["decision"] not in play.ACTIONABLE:
                # direct/fail/unknown：缓存不该存在，让前端走原逻辑
                self._send_json(409, {"error": "no conversion for this file",
                                      "decision": d["decision"]})
                return
            cache = play.get_ready(target)
            if cache is not None:
                target = cache
            else:
                st = play.build_status(target)
                if st == "building":
                    self._send_json(202, {"status": "building", "fmt": fmt})
                    return
                self._send_json(409, {"error": "conversion not ready",
                                      "status": st})
                return
        elif track is not None:
            # v2.5.0：音轨缓存文件优先（有 track 且缓存已就绪时）
            from .tracks import build_status, get_ready

            status = build_status(target, track)
            cache = get_ready(target, track)
            if status == "building":
                self._send_json(202, {"status": "building", "track": track})
                return
            if cache is None:
                # 未开始/失败：先播原文件（前端会收到 200 原文件流）
                target_ = target
            else:
                target_ = cache
            # 用缓存文件继续走下方统一 stat + Range 流
            target = target_
        # 注意：track/fmt 都为 None（默认）时 target 就是原文件，走原有逻辑。

        # v1.8.0 修正 TOCTOU：原先先 is_file() 再 stat()，两次系统调用之间
        # 文件可能被删。现在只 stat 一次，用它同时判断「存在」与「大小」。
        try:
            size = stat_path(target).st_size
        except OSError as exc:
            self._send_json(404, {"error": f"not found: {exc}"})
            return
        if size <= 0:
            self._send_json(404, {"error": "not found"})
            return
        # 历史记录：谁、在什么时候、看了哪个文件（v1.4.0）。
        # 放在这之后（确认文件真的存在、真能播），避免把 404/403 也记成播放。
        try:
            from urllib.parse import unquote as _unquote

            from .history import record as _record_history
            # 浏览器发的路径是 URL 编码态（"01%20%E7%94%B5%E5%BD%B1"），
            # 历史里必须记解码后的中文，否则界面上没法看。
            _rel_decoded = _unquote(rel)
            _parts = [x for x in _rel_decoded.replace("\\", "/").split("/") if x]
            _record_history(
                self.client_address[0] if self.client_address else "",
                "play", _rel_decoded, _parts[0] if _parts else "")
        except Exception:
            pass
        range_header = self.headers.get("Range")
        if range_header is not None and not range_header.strip():
            range_header = None
        try:
            span = parse_range_header(range_header, size)
        except RangeNotSatisfiable as exc:
            self._send_range_error(416, exc.size)
            return
        if span is None:
            if range_header:
                # 带了 Range 但无法解析（如 bytes=abc、多段 Range）：
                # 直接返回 416 更安全（避免把整个 3.6GB 视频推给客户端导致卡死）。
                print(f"[avwall] Range 无法解析，返回 416：{range_header!r}", flush=True)
                self._send_range_error(416, size)
                return
            self._stream(target, 200, 0, size - 1, size, with_range_header=False)
        elif span == (0, size - 1):
            # 覆盖整个文件的 Range（bytes=0- / bytes=0-(size-1)）等价于全量请求，
            # 按 RFC 7233 回 200 全量，避免「206 但 Content-Range 覆盖全文件」的歧义。
            # v1.3.0 更正：v1.2.1 注释里「这会导致 Chromium 反复 abort、播放闪烁」
            # 的说法已被实测证伪——真正的根因是 index.html 里 progress 回调中调用
            # load() 造成的自打断死循环。实测 Chromium 收到本 200 响应后，会自行再发
            # 尾部 Range 取回文件末尾的 moov，起播正常。
            self._stream(target, 200, 0, size - 1, size, with_range_header=False)
        else:
            self._stream(target, 206, span[0], span[1], size, with_range_header=True)
        # 注意：媒体流刻意【不】强制关闭连接。
        # 安卓 / 鸿蒙（Chromium/ArkWeb）的媒体框架会在同一连接上连续发多个
        # Range 请求（seek / 续传）；若每个 Range 都 Connection: close，
        # 部分手机会在第一个分片后停止拉流而表现为「卡住 / 转圈」。
        # 交给框架按客户端 HTTP/1.1 的 Connection 头决定是否复用即可。

    def _serve_thumb(self, rel: str) -> None:
        """``GET /thumb/<相对路径>``：返回缩略图（带大小上限）。"""
        try:
            target = resolve_within(self._root, rel)
        except PathSecurityError:
            self._send_json(403, {"error": "forbidden"})
            return
        if target.suffix.lower() not in THUMB_EXTS:
            self._send_json(403, {"error": "file type not allowed"})
            return
        # v1.8.0 修正：先 stat 再读，缺大小上限。
        # 静态资源有 64MB 保护，缩略图却没有 → 一张超大图会一次性读进内存。
        try:
            size = stat_path(target).st_size
        except OSError as exc:
            self._send_json(404, {"error": f"not found: {exc}"})
            return
        if size > MAX_THUMB_BYTES:
            self._send_json(413, {"error": "thumbnail too large"})
            return
        try:
            body = target.read_bytes()
        except OSError as exc:
            self._send_json(500, {"error": f"read failed: {exc}"})
            return
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if target.suffix.lower() == ".webp":
            content_type = "image/webp"
        self._send_bytes(200, content_type, body)

    # ---- 流式输出 ----

    def _stream(self, target: Path, status: int, start: int, end: int, size: int,
                with_range_header: bool) -> None:
        """按 1MB 分块流式发送文件区间；客户端断开静默吞掉。

        v1.8.0 修正（严重：客户端永久挂死）：
        原先是「先 ``end_headers()`` 承诺 Content-Length，再 ``open()``」。
        若文件在 stat 之后消失（NAS 抖动、文件被删），会**先发出完整响应头
        再抛异常**，于是：

        * 客户端收到 ``Content-Length: 65536`` 但 body 收到 0 字节，
          连接又不关闭 → 客户端一直等，表现为「视频卡住转圈」；
        * 读取中途被截断（读满 4096 后 ``read()`` 返回 b''），
          剩下 61440 字节永远收不到，连接保持打开。

        现在改成：**先 open 成功再发头**（文件拿不到就直接 404/500，
        不发任何承诺了长度的头），并用 ``fstat`` 取真实长度；
        若实际发出的字节数少于声明长度，**强制关闭连接**，
        客户端会立刻感知到「连接断了」而不是无限等待。
        """
        length = end - start + 1
        suffix = target.suffix.lower()
        content_type = _MEDIA_MIME.get(
            suffix, mimetypes.guess_type(target.name)[0] or "video/mp4"
        )

        # ---- 先打开文件，失败就根本不发头 ----
        try:
            handle = open_path(target, "rb")
        except OSError as exc:
            self._send_json(404, {"error": f"open failed: {exc}"})
            return
        try:
            try:
                real_size = os.fstat(handle.fileno()).st_size
            except OSError:
                real_size = size
            # 文件被截短了（比 stat 时小）→ 以实际大小为准，
            # 避免声明的长度大于能发出的字节数。
            if start >= real_size:
                self._send_json(404, {"error": "file truncated or empty"})
                return
            if end >= real_size:
                end = real_size - 1
                length = end - start + 1

            try:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(length))
                self.send_header("Accept-Ranges", "bytes")
                if with_range_header:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.send_header("Cache-Control", "no-store")
                origin = self._cors_header_value()
                if origin:
                    self.send_header("Access-Control-Allow-Origin", origin)
                    self.send_header("Vary", "Origin")
                self.end_headers()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                self.close_connection = True
                return

            if self.command == "HEAD":
                # HEAD 语义：有头无 body；主动关闭连接，避免客户端按
                # Content-Length 继续等待 body 而挂住
                self.close_connection = True
                return

            handle.seek(start)
            remaining = length
            sent = 0
            while remaining > 0:
                chunk = handle.read(min(CHUNK_SIZE, remaining))
                if not chunk:
                    # 提前 EOF：实际发得比声明的少 → 必须关连接，
                    # 否则客户端会一直等剩下的字节（永久挂死）。
                    print(f"[avwall] 媒体流提前结束：{target.name} "
                          f"已发 {sent}/{length} 字节，关闭连接。", flush=True)
                    self.close_connection = True
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)
                sent += len(chunk)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True
        except OSError:
            self.close_connection = True
        finally:
            try:
                handle.close()
            except OSError:
                pass

    def _send_range_error(self, status: int, size: int) -> None:
        """416 响应：带 ``Content-Range: bytes */<size>``。"""
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Cache-Control", "no-store")
            origin = self._cors_header_value()
            if origin:
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
            body = json.dumps({"error": "range not satisfiable"},
                              ensure_ascii=False).encode("utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command == "HEAD":
                self.close_connection = True
                return
            self.wfile.write(body)
        except OSError:
            self.close_connection = True
